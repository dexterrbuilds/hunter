"""Build provider adapters from validated configuration."""

# Configuration parsing reports actionable field-level messages.
# ruff: noqa: TRY003

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from execution.errors import ErrorClassification, ExecutionError
from execution.metrics import LatencyBudgets
from execution.providers.adapters import (
    HeliusSenderMaxSubmitter,
    HeliusSenderSubmitter,
    JitoTransactionSubmitter,
    JsonRpcTransactionSubmitter,
    TritonJetSubmitter,
)
from execution.providers.config import (
    BroadcastMode,
    ExecutionRoutingConfig,
    ProviderEndpoint,
    ProviderKind,
    ProviderRole,
)
from execution.routing import AttemptCallback, SubmissionRouter

if TYPE_CHECKING:
    from execution.ports import TransactionSubmitter


def routing_config_from_dict(
    value: dict[str, Any] | None,
) -> ExecutionRoutingConfig:
    """Parse optional YAML execution config without reinterpreting old fields."""
    if not value:
        return ExecutionRoutingConfig()
    providers_value = value.get("providers", [])
    if not isinstance(providers_value, list):
        raise TypeError("execution.providers must be a list")
    providers = tuple(_provider_from_dict(item) for item in providers_value)
    try:
        mode = BroadcastMode(value.get("mode", BroadcastMode.SINGLE.value))
    except ValueError as error:
        raise ValueError(
            "execution.mode must be single, race, hedged, or fallback"
        ) from error
    return ExecutionRoutingConfig(
        enabled=_boolean(value, "enabled", default=False),
        mode=mode,
        hedge_delay_ms=_non_negative_int(value, "hedge_delay_ms", 75),
        maximum_blockhash_age_ms=_positive_int(
            value, "maximum_blockhash_age_ms", 30_000
        ),
        maximum_combined_fee_lamports=_optional_non_negative_int(
            value, "maximum_combined_fee_lamports"
        ),
        execution_variant=_string(value, "execution_variant", "standard"),
        jito_tip_lamports=_non_negative_int(value, "jito_tip_lamports", 0),
        jito_tip_account=(_string(value, "jito_tip_account", "") or None),
        latency_budgets=_latency_budgets(value),
        providers=providers,
    )


def build_submission_router(
    config: ExecutionRoutingConfig,
    *,
    attempt_callback: AttemptCallback | None = None,
) -> SubmissionRouter | None:
    """Create submit-only adapters; read/status roles remain separately selectable."""
    if not config.enabled:
        return None
    endpoints = config.for_role(ProviderRole.SUBMIT)
    submitters = tuple(_submitter(endpoint) for endpoint in endpoints)
    return SubmissionRouter(
        submitters,
        mode=config.mode,
        hedge_delay_ms=config.hedge_delay_ms,
        attempt_callback=attempt_callback,
    )


def _submitter(endpoint: ProviderEndpoint) -> TransactionSubmitter:
    if endpoint.kind == ProviderKind.STANDARD_RPC:
        return JsonRpcTransactionSubmitter(endpoint)
    if endpoint.kind == ProviderKind.HELIUS_SENDER:
        return HeliusSenderSubmitter(endpoint)
    if endpoint.kind == ProviderKind.HELIUS_SENDER_MAX:
        return HeliusSenderMaxSubmitter(endpoint)
    if endpoint.kind == ProviderKind.JITO:
        return JitoTransactionSubmitter(endpoint)
    if endpoint.kind in {ProviderKind.TRITON_JET, ProviderKind.SWQOS}:
        return TritonJetSubmitter(endpoint)
    raise ExecutionError(
        ErrorClassification.UNSUPPORTED_PROVIDER,
        f"unsupported provider kind: {endpoint.kind}",
    )


def _provider_from_dict(value: object) -> ProviderEndpoint:
    if not isinstance(value, dict):
        raise TypeError("each execution provider must be a mapping")
    try:
        kind = ProviderKind(value["kind"])
        provider_id = str(value["id"])
        endpoint = str(value["endpoint"])
    except KeyError as error:
        raise ValueError(f"execution provider is missing {error.args[0]}") from error
    roles_value = value.get("roles", [ProviderRole.SUBMIT.value])
    if not isinstance(roles_value, list):
        raise TypeError(f"execution provider {provider_id} roles must be a list")
    try:
        roles = frozenset(ProviderRole(role) for role in roles_value)
    except ValueError as error:
        raise ValueError(
            f"execution provider {provider_id} has an invalid role"
        ) from error
    headers = value.get("headers", {})
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in headers.items()
    ):
        raise ValueError(f"execution provider {provider_id} headers must be strings")
    return ProviderEndpoint(
        provider_id=provider_id,
        kind=kind,
        endpoint=endpoint,
        priority=_non_negative_int(value, "priority", 100),
        roles=roles,
        enabled=_boolean(value, "enabled", default=True),
        headers=dict(headers),
        request_timeout_ms=_positive_int(value, "request_timeout_ms", 5_000),
        skip_preflight=_boolean(value, "skip_preflight", default=True),
        max_retries=_non_negative_int(value, "max_retries", 0),
        bundle_only=_boolean(value, "bundle_only", default=False),
        minimum_tip_lamports=_non_negative_int(value, "minimum_tip_lamports", 0),
        maximum_tip_lamports=_non_negative_int(value, "maximum_tip_lamports", 0),
        warmup_endpoint=(
            str(value["warmup_endpoint"])
            if value.get("warmup_endpoint") is not None
            else None
        ),
        region=(_string(value, "region", "") or None),
        required=_boolean(value, "required", default=False),
    )


def _boolean(value: dict[str, Any], key: str, *, default: bool) -> bool:
    item = value.get(key, default)
    if not isinstance(item, bool):
        raise TypeError(f"{key} must be true or false")
    return item


def _non_negative_int(value: dict[str, Any], key: str, default: int) -> int:
    item = value.get(key, default)
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return item


def _positive_int(value: dict[str, Any], key: str, default: int) -> int:
    item = _non_negative_int(value, key, default)
    if item == 0:
        raise ValueError(f"{key} must be positive")
    return item


def _optional_non_negative_int(value: dict[str, Any], key: str) -> int | None:
    item = value.get(key)
    if item is None:
        return None
    return _non_negative_int(value, key, 0)


def _string(value: dict[str, Any], key: str, default: str) -> str:
    item = value.get(key, default)
    if not isinstance(item, str):
        raise TypeError(f"{key} must be a string")
    return item


def _latency_budgets(value: dict[str, Any]) -> LatencyBudgets:
    configured = value.get("latency_budgets")
    if configured is None:
        return LatencyBudgets()
    if not isinstance(configured, dict):
        raise TypeError("execution.latency_budgets must be a mapping")
    allowed = {
        "detection_processing_ms",
        "quote_generation_ms",
        "blockhash_retrieval_ms",
        "transaction_build_ms",
        "signing_ms",
        "submission_rtt_ms",
    }
    unknown = set(configured) - allowed
    if unknown:
        raise ValueError(f"unknown execution latency budget: {sorted(unknown)[0]}")
    parsed: dict[str, float | None] = {}
    for key in allowed:
        item = configured.get(key)
        if item is None:
            parsed[key] = None
        elif isinstance(item, bool) or not isinstance(item, int | float) or item <= 0:
            raise ValueError(f"execution.latency_budgets.{key} must be positive")
        else:
            parsed[key] = float(item)
    return LatencyBudgets(**parsed)
