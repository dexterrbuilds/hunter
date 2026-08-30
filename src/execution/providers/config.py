"""Validated configuration for provider roles and submission routing."""

# Configuration validation benefits from direct, actionable error messages.
# ruff: noqa: C901, PLR0912, TRY003

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from execution.errors import ErrorClassification, ExecutionError
from execution.metrics import LatencyBudgets
from execution.providers.capabilities import (
    HELIUS_SENDER_CAPABILITIES,
    HELIUS_SENDER_MAX_CAPABILITIES,
    JITO_CAPABILITIES,
    STANDARD_CAPABILITIES,
    SWQOS_CAPABILITIES,
)
from utils.redaction import endpoint_identifier, register_config_secrets

if TYPE_CHECKING:
    from execution.providers.capabilities import ProviderCapabilities


class ProviderKind(StrEnum):
    """Supported execution transports."""

    STANDARD_RPC = "standard_rpc"
    HELIUS_SENDER = "helius_sender"
    HELIUS_SENDER_MAX = "helius_sender_max"
    JITO = "jito"
    TRITON_JET = "triton_jet"
    SWQOS = "swqos"


class ProviderRole(StrEnum):
    """RPC functions that may be routed independently."""

    ACCOUNT_READ = "account_read"
    BLOCKHASH = "blockhash"
    SUBMIT = "submit"
    CONFIRM = "confirm"
    WEBSOCKET = "websocket"


class BroadcastMode(StrEnum):
    """Controlled delivery modes for compatible signed transactions."""

    SINGLE = "single"
    RACE = "race"
    HEDGED = "hedged"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class ProviderEndpoint:
    """One credential-bearing endpoint plus credential-free identity."""

    provider_id: str
    kind: ProviderKind
    endpoint: str = field(repr=False)
    priority: int = 100
    roles: frozenset[ProviderRole] = field(
        default_factory=lambda: frozenset({ProviderRole.SUBMIT})
    )
    enabled: bool = True
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    request_timeout_ms: int = 5_000
    skip_preflight: bool = True
    max_retries: int = 0
    bundle_only: bool = False
    minimum_tip_lamports: int = 0
    maximum_tip_lamports: int = 0
    warmup_endpoint: str | None = field(default=None, repr=False)
    region: str | None = None
    required: bool = False

    def __post_init__(self) -> None:
        if not self.provider_id or any(char.isspace() for char in self.provider_id):
            raise ValueError("provider_id must be a stable non-blank identifier")
        if self.priority < 0:
            raise ValueError("provider priority must be non-negative")
        if self.request_timeout_ms <= 0:
            raise ValueError("provider request timeout must be positive")
        if self.max_retries < 0:
            raise ValueError("provider max_retries must be non-negative")
        if self.minimum_tip_lamports < 0 or self.maximum_tip_lamports < 0:
            raise ValueError("tip bounds must be non-negative")
        if (
            self.maximum_tip_lamports
            and self.minimum_tip_lamports > self.maximum_tip_lamports
        ):
            raise ValueError("minimum tip cannot exceed maximum tip")
        _validate_endpoint(self.endpoint)
        if self.warmup_endpoint is not None:
            _validate_endpoint(self.warmup_endpoint)
        non_submit_roles = self.roles - {ProviderRole.SUBMIT}
        if non_submit_roles and self.kind != ProviderKind.STANDARD_RPC:
            raise ValueError(
                "specialized sender endpoints can only have the submit role"
            )
        scheme = urlsplit(self.endpoint).scheme
        if ProviderRole.WEBSOCKET in self.roles and scheme not in {"ws", "wss"}:
            raise ValueError("websocket role requires a ws:// or wss:// endpoint")
        if self.roles - {ProviderRole.WEBSOCKET} and scheme not in {"http", "https"}:
            raise ValueError("RPC roles require an http:// or https:// endpoint")
        register_config_secrets(
            {
                "endpoint": self.endpoint,
                "warmup_endpoint": self.warmup_endpoint,
                "headers": self.headers,
            }
        )

    @property
    def endpoint_id(self) -> str:
        """Stable identifier that never includes credentials or URL paths."""
        return endpoint_identifier(self.endpoint)

    def supports(self, role: ProviderRole) -> bool:
        """Whether this endpoint is enabled for a routing role."""
        return self.enabled and role in self.roles

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Return adapter requirements without exposing vendor details upstream."""
        return {
            ProviderKind.STANDARD_RPC: STANDARD_CAPABILITIES,
            ProviderKind.HELIUS_SENDER: HELIUS_SENDER_CAPABILITIES,
            ProviderKind.HELIUS_SENDER_MAX: HELIUS_SENDER_MAX_CAPABILITIES,
            ProviderKind.JITO: JITO_CAPABILITIES,
            ProviderKind.TRITON_JET: SWQOS_CAPABILITIES,
            ProviderKind.SWQOS: SWQOS_CAPABILITIES,
        }[self.kind]


@dataclass(frozen=True, slots=True)
class ExecutionRoutingConfig:
    """Execution routing and fee safety settings."""

    enabled: bool = False
    mode: BroadcastMode = BroadcastMode.SINGLE
    hedge_delay_ms: int = 75
    maximum_blockhash_age_ms: int = 30_000
    maximum_combined_fee_lamports: int | None = None
    execution_variant: str = "standard"
    jito_tip_lamports: int = 0
    jito_tip_account: str | None = field(default=None, repr=False)
    latency_budgets: LatencyBudgets = field(default_factory=LatencyBudgets)
    providers: tuple[ProviderEndpoint, ...] = ()

    def __post_init__(self) -> None:
        if self.hedge_delay_ms < 0:
            raise ValueError("hedge delay must be non-negative")
        if self.maximum_blockhash_age_ms <= 0:
            raise ValueError("maximum blockhash age must be positive")
        if (
            self.maximum_combined_fee_lamports is not None
            and self.maximum_combined_fee_lamports < 0
        ):
            raise ValueError("maximum combined fee must be non-negative")
        if self.execution_variant not in {
            "standard",
            "jito_tipped",
            "helius_sender_tipped",
            "sender_max_tipped",
        }:
            raise ValueError("unsupported execution variant")
        if self.jito_tip_lamports < 0:
            raise ValueError("Jito tip must be non-negative")
        if self.jito_tip_lamports and self.execution_variant == "standard":
            raise ValueError("a Jito tip requires an explicitly tipped variant")
        if self.jito_tip_lamports and not self.jito_tip_account:
            raise ValueError("a tipped variant requires a configured tip account")
        if not self.jito_tip_lamports and self.execution_variant != "standard":
            raise ValueError("a tipped execution variant requires a positive tip")
        identifiers = [item.provider_id for item in self.providers]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("provider IDs must be unique")
        if self.enabled and not self.for_role(ProviderRole.SUBMIT):
            raise ValueError("enabled execution routing needs a submit provider")
        if self.jito_tip_lamports:
            if self.maximum_combined_fee_lamports is None:
                raise ValueError(
                    "a tipped variant requires maximum_combined_fee_lamports"
                )
            for provider in self.for_role(ProviderRole.SUBMIT):
                if (
                    not provider.capabilities.requires_tip
                    and provider.kind != ProviderKind.JITO
                ):
                    continue
                if provider.minimum_tip_lamports <= 0:
                    raise ValueError(
                        f"tipped provider {provider.provider_id} requires a minimum tip"
                    )
                if provider.maximum_tip_lamports <= 0:
                    raise ValueError(
                        f"tipped provider {provider.provider_id} requires a maximum tip"
                    )
                if self.jito_tip_lamports < provider.minimum_tip_lamports:
                    raise ValueError(
                        f"tip is below {provider.provider_id} configured minimum"
                    )
                if self.jito_tip_lamports > provider.maximum_tip_lamports:
                    raise ValueError(
                        f"tip exceeds {provider.provider_id} configured maximum"
                    )

    def for_role(self, role: ProviderRole) -> tuple[ProviderEndpoint, ...]:
        """Return enabled endpoints for a role ordered by configured priority."""
        if not self.enabled:
            return ()
        return tuple(
            sorted(
                (item for item in self.providers if item.supports(role)),
                key=lambda item: (item.priority, item.provider_id),
            )
        )

    def validate_fee_exposure(
        self,
        *,
        base_fee_lamports: int | None,
        priority_fee_lamports: int,
        jito_tip_lamports: int,
        rent_lamports: int,
        other_known_cost_lamports: int = 0,
    ) -> int | None:
        """Validate known fee components without obscuring their denomination."""
        components = (
            priority_fee_lamports,
            jito_tip_lamports,
            rent_lamports,
            other_known_cost_lamports,
        )
        if any(value < 0 for value in components):
            raise ValueError("fee components must be non-negative")
        if base_fee_lamports is None:
            known_without_base = sum(components)
            maximum = self.maximum_combined_fee_lamports
            if maximum is not None and known_without_base > maximum:
                raise ExecutionError(
                    ErrorClassification.RISK_LIMIT_EXCEEDED,
                    "known fee exposure excluding unknown base fee exceeds "
                    f"configured maximum {maximum}",
                )
            return None
        total = base_fee_lamports + sum(components)
        maximum = self.maximum_combined_fee_lamports
        if maximum is not None and total > maximum:
            raise ExecutionError(
                ErrorClassification.RISK_LIMIT_EXCEEDED,
                f"combined fee exposure {total} exceeds configured maximum {maximum}",
            )
        return total


def _validate_endpoint(endpoint: str) -> None:
    parts = urlsplit(endpoint)
    if parts.scheme not in {"http", "https", "ws", "wss"} or not parts.hostname:
        raise ValueError("provider endpoint must be an absolute HTTP or WebSocket URL")


def role_endpoints(
    config: ExecutionRoutingConfig,
) -> dict[ProviderRole, tuple[ProviderEndpoint, ...]]:
    """Expose explicit read/write role separation without routing policy magic."""
    if not config.enabled:
        return dict.fromkeys(ProviderRole, ())
    return {role: config.for_role(role) for role in ProviderRole}
