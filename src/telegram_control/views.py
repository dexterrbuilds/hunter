"""Concise, secret-safe Telegram rendering over Hunter application models."""

# Views intentionally accept read-only application-shaped objects.
# ruff: noqa: ANN401, FBT001

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import fields, is_dataclass
from decimal import Decimal
from typing import Any

from core.pubkeys import is_sol_paired
from utils.redaction import REDACTED, sanitize_text

TELEGRAM_MESSAGE_LIMIT = 4096
PAGE_LIMIT = 3600
_SENSITIVE_PARTS = (
    "token",
    "secret",
    "private",
    "credential",
    "password",
    "endpoint",
    "url",
)


def split_message(text: str, limit: int = PAGE_LIMIT) -> tuple[str, ...]:
    """Split at line boundaries and never exceed Telegram's hard limit."""
    safe = sanitize_text(text)
    if len(safe) <= limit:
        return (safe,)
    pages: list[str] = []
    current: list[str] = []
    size = 0
    for source_line in safe.splitlines() or [safe]:
        line = source_line[: TELEGRAM_MESSAGE_LIMIT - 1]
        extra = len(line) + int(bool(current))
        if current and size + extra > limit:
            pages.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += len(line) + int(len(current) > 1)
    if current:
        pages.append("\n".join(current))
    return tuple(pages)


def status_text(status: Any, runtime_mode: str) -> str:
    listeners = _component_summary(getattr(status, "listeners", ()))
    providers = _component_summary(getattr(status, "providers", ()))
    tracked = getattr(status, "tracked_wallet", None)
    tracked_state = getattr(getattr(tracked, "state", None), "value", "disabled")
    return "\n".join(
        (
            "Hunter status",
            f"Ready: {_yes(getattr(status, 'application_ready', False))}",
            f"Lifecycle: {_value(getattr(status, 'application_state', 'unknown'))}",
            f"Mode: {runtime_mode}",
            f"Trading enabled: {_yes(getattr(status, 'trading_enabled', False))}",
            f"Kill switch: {_yes(getattr(status, 'kill_switch', False))}",
            f"Entries allowed: {_yes(getattr(status, 'entries_allowed', False))}",
            "Defensive exits allowed: "
            f"{_yes(getattr(status, 'defensive_exits_allowed', False))}",
            f"Open positions: {getattr(status, 'open_positions', 0)}",
            f"Tracked wallets: {tracked_state}",
            f"Feeds: {listeners}",
            f"Providers: {providers}",
        )
    )


def position_text(position: Any, *, detailed: bool = False) -> str:
    accounting = position.accounting
    quote = accounting.quote_mint
    lines = [
        f"{str(accounting.token_mint)[:8]}…  {accounting.status.value}",
        f"Position: {accounting.position_id}",
        f"Mint: {accounting.token_mint}",
        f"Quantity raw: {accounting.remaining_quantity_raw}",
        f"Cost basis raw: {accounting.remaining_cost_basis_raw}",
        f"Realized gross raw: {accounting.realized_gross_pnl_raw}",
        f"Realized net raw: {_known(accounting.realized_net_pnl_raw)}",
        f"Quote mint: {quote}",
    ]
    if detailed:
        lines.extend(
            (
                f"Entry network fee (lamports): {_known(accounting.entry_network_fee_lamports)}",
                f"Exit network fee (lamports): {_known(accounting.exit_network_fee_lamports)}",
                f"Entry priority fee (lamports): {_known(accounting.entry_priority_fee_lamports)}",
                f"Exit priority fee (lamports): {_known(accounting.exit_priority_fee_lamports)}",
                f"TP bps: {_known(position.take_profit_bps)}",
                f"SL bps: {_known(position.stop_loss_bps)}",
                f"Trailing bps: {_known(position.trailing_stop_bps)}",
                f"Created: {position.created_at or 'unknown'}",
            )
        )
    if not is_sol_paired(quote):
        lines.append("SOL fees remain separate from quote-denominated PnL.")
    return "\n".join(lines)


def execution_text(execution: Any) -> str:
    return "\n".join(
        (
            f"Execution: {execution.logical_execution_id}",
            f"Side: {execution.side}",
            f"State: {_value(execution.state)}",
            f"Position: {execution.position_id or 'unlinked'}",
            f"Signature: {execution.signature or 'not submitted'}",
            f"Attempt: {execution.submission_attempt}",
            "Error: "
            f"{_value(execution.error_classification) if execution.error_classification else 'none'}",
            f"Updated: {execution.updated_at}",
        )
    )


def safe_settings_text(values: Mapping[str, Any]) -> str:
    lines = ["Effective non-secret settings"]
    for key, value in sorted(values.items()):
        if any(part in key.lower() for part in _SENSITIVE_PARTS):
            rendered = REDACTED
        elif isinstance(value, Mapping | list | tuple | set):
            rendered = "configured"
        else:
            rendered = sanitize_text(value)
        lines.append(f"{key}: {rendered}")
    return "\n".join(lines)


def dataclass_mapping(value: Any) -> dict[str, Any]:
    if not is_dataclass(value):
        return {}
    return {item.name: getattr(value, item.name) for item in fields(value)}


def decimal_text(raw: int, decimals: int) -> str:
    return format(Decimal(raw) / (Decimal(10) ** decimals), "f")


def _component_summary(items: Iterable[Any]) -> str:
    values = [f"{item.name}={_value(item.state)}" for item in items]
    return ", ".join(values) if values else "none"


def _value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def _known(value: Any) -> str:
    return "unknown" if value is None else str(value)
