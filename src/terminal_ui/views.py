"""Pure terminal view rendering with explicit unavailable-value semantics."""

from __future__ import annotations

from datetime import UTC, datetime

from terminal_ui.models import DisplayValue, TerminalSnapshot, TerminalView, ValueState
from utils.redaction import sanitize_text


def render_view(snapshot: TerminalSnapshot, view: TerminalView) -> str:
    """Render one snapshot without performing I/O or querying application state."""
    renderers = {
        TerminalView.OVERVIEW: _overview,
        TerminalView.RUNTIME: _runtime,
        TerminalView.FEEDS: _feeds,
        TerminalView.PROVIDERS: _providers,
        TerminalView.POSITIONS: _positions,
        TerminalView.POSITION_DETAILS: _position_details,
        TerminalView.EXECUTIONS: _executions,
        TerminalView.TRANSACTION: _transaction,
        TerminalView.WALLETS: _wallets,
        TerminalView.TRACKED_WALLETS: _tracked_wallets,
        TerminalView.WALLET_FLEET: _wallet_fleet,
        TerminalView.TOKEN_LAUNCHES: _token_launches,
        TerminalView.RISK: _risk,
        TerminalView.EVENTS: _events,
        TerminalView.LATENCY: _latency,
        TerminalView.SETTINGS: _settings,
    }
    return renderers[view](snapshot)


def navigation_labels() -> tuple[tuple[TerminalView, str], ...]:
    """Return stable navigation ordering for ordinary terminal sizes."""
    return (
        (TerminalView.OVERVIEW, "Overview"),
        (TerminalView.RUNTIME, "Runtime & recovery"),
        (TerminalView.FEEDS, "Detection feeds"),
        (TerminalView.PROVIDERS, "Providers & routing"),
        (TerminalView.POSITIONS, "Positions"),
        (TerminalView.POSITION_DETAILS, "Position details"),
        (TerminalView.EXECUTIONS, "Execution history"),
        (TerminalView.TRANSACTION, "Transaction lookup"),
        (TerminalView.WALLETS, "Wallets & balances"),
        (TerminalView.TRACKED_WALLETS, "Tracked wallets"),
        (TerminalView.WALLET_FLEET, "Wallet fleet"),
        (TerminalView.TOKEN_LAUNCHES, "Token launches"),
        (TerminalView.RISK, "Risk configuration"),
        (TerminalView.EVENTS, "Recent events & errors"),
        (TerminalView.LATENCY, "Latency & landing"),
        (TerminalView.SETTINGS, "Settings & diagnostics"),
    )


def _overview(value: TerminalSnapshot) -> str:
    status = value.status
    latest = value.recent_events[-1] if value.recent_events else None
    return _section(
        "HUNTER OPERATOR OVERVIEW",
        (
            ("Lifecycle", _enum(status.application_state)),
            ("Application ready", _yes_no(status.application_ready)),
            ("Recovery complete", _yes_no(status.recovery_complete)),
            ("Trading enabled", _yes_no(status.trading_enabled)),
            ("Kill switch", "ACTIVE" if status.kill_switch else "inactive"),
            ("New entries", "authorized" if status.entries_allowed else "blocked"),
            (
                "Defensive exits",
                "authorized" if status.defensive_exits_allowed else "blocked",
            ),
            ("Open positions", str(status.open_positions)),
            ("Pending executions", str(status.pending_executions)),
            ("Route mode", value.routing_mode),
            ("Wallet data", value.wallet_freshness.value),
            ("Telemetry data", value.telemetry_freshness.value),
            (
                "Latest event",
                "unavailable"
                if latest is None
                else f"{latest.event_type} ({_age(latest.occurred_at)})",
            ),
            (
                "UI event loss",
                f"{value.event_drops} terminal / {status.dropped_events} application",
            ),
        ),
    ) + _errors(value)


def _runtime(value: TerminalSnapshot) -> str:
    status = value.status
    lines = [
        f"State: {_enum(status.application_state)}",
        f"Ready: {_yes_no(status.application_ready)}",
        f"Recovery: {'complete' if status.recovery_complete else 'incomplete'}",
        f"Database: {'ready' if status.database_ready else 'not ready'}",
        f"Bots: {', '.join(status.active_bots) or 'none'}",
        f"Reconciliation required: {status.pending_reconciliation}",
        "",
        "Startup timings",
    ]
    lines.extend(
        f"  {item.phase:<34} {item.duration_ms:>10.3f} ms"
        for item in status.startup_timings
    )
    if not status.startup_timings:
        lines.append("  unmeasured")
    return "\n".join(lines) + _errors(value)


def _feeds(value: TerminalSnapshot) -> str:
    lines = ["DETECTION FEEDS", ""]
    for item in value.status.listeners:
        reason = f" — {sanitize_text(item.reason)}" if item.reason else ""
        lines.append(
            f"{item.name:<24} {_enum(item.state):<12} "
            f"{'required' if item.required else 'optional'}{reason}"
        )
    if not value.status.listeners:
        lines.append("not configured")
    latest = value.recent_events[-1] if value.recent_events else None
    lines.extend(
        (
            "",
            "Latest observed event: "
            + (
                "unavailable"
                if latest is None
                else f"{latest.event_type} at {latest.occurred_at.isoformat()}"
            ),
            f"Application dropped events: {value.status.dropped_events}",
            f"Terminal dropped events: {value.event_drops}",
            "Detection-to-decision: "
            + _display(value.performance.detection_to_decision_ms, "ms"),
        )
    )
    return "\n".join(lines)


def _providers(value: TerminalSnapshot) -> str:
    lines = [f"ROUTING MODE: {value.routing_mode}", ""]
    for item in value.status.providers:
        reason = f" — {sanitize_text(item.reason)}" if item.reason else ""
        lines.append(
            f"{item.name:<24} {_enum(item.state):<12} "
            f"{'required' if item.required else 'optional'}{reason}"
        )
    if not value.status.providers:
        lines.append(
            "Provider details unavailable or no execution providers configured"
        )
    lines.extend(
        (
            "",
            f"Blockhash cache: {_optional_ready(value.status.blockhash_ready)}",
            f"Priority-fee cache: {_optional_ready(value.status.fee_cache_ready)}",
            f"Jito tip cache: {_optional_ready(value.status.tip_cache_ready)}",
            "Provider endpoints are intentionally omitted.",
        )
    )
    return "\n".join(lines)


def _positions(value: TerminalSnapshot) -> str:
    lines = [
        f"POSITIONS — offset {value.position_offset}",
        "ID                    MINT                 STATE              QTY RAW       COST RAW",
    ]
    for item in value.positions:
        accounting = getattr(item, "accounting", None)
        if accounting is None:
            continue
        lines.append(
            f"{_clip(accounting.position_id, 20):<20}  "
            f"{_clip(str(accounting.token_mint), 20):<20} "
            f"{_enum(accounting.status):<18} "
            f"{accounting.remaining_quantity_raw:>12} "
            f"{accounting.remaining_cost_basis_raw:>12}"
        )
    if not value.positions:
        lines.append("No positions in this page.")
    lines.append(
        "Current valuation/unrealized PnL: unavailable without a fresh price source."
    )
    return "\n".join(lines)


def _position_details(value: TerminalSnapshot) -> str:
    if not value.positions:
        return "POSITION DETAILS\n\nNo position selected in the current page."
    item = value.positions[0]
    accounting = item.accounting
    return _section(
        "POSITION DETAILS — first row (selection navigation is read-only)",
        (
            ("Position ID", accounting.position_id),
            ("Mint", str(accounting.token_mint)),
            ("Quote mint", str(accounting.quote_mint)),
            ("Lifecycle", _enum(accounting.status)),
            ("Acquired raw", str(accounting.acquired_quantity_raw)),
            ("Remaining raw", str(accounting.remaining_quantity_raw)),
            ("Quote cost raw", str(accounting.quote_cost_raw)),
            ("Remaining basis raw", str(accounting.remaining_cost_basis_raw)),
            ("Realized gross raw", str(accounting.realized_gross_pnl_raw)),
            (
                "Realized net raw",
                "unknown"
                if accounting.realized_net_pnl_raw is None
                else str(accounting.realized_net_pnl_raw),
            ),
            (
                "Entry network fee (lamports)",
                _known(accounting.entry_network_fee_lamports),
            ),
            (
                "Exit network fee (lamports)",
                _known(accounting.exit_network_fee_lamports),
            ),
            (
                "Entry priority fee (lamports)",
                _known(accounting.entry_priority_fee_lamports),
            ),
            (
                "Exit priority fee (lamports)",
                _known(accounting.exit_priority_fee_lamports),
            ),
            ("TP (bps)", _known(getattr(item, "take_profit_bps", None))),
            ("SL (bps)", _known(getattr(item, "stop_loss_bps", None))),
            ("Trailing stop (bps)", _known(getattr(item, "trailing_stop_bps", None))),
            ("Valuation", "unavailable (no fresh price query from TUI)"),
        ),
    )


def _executions(value: TerminalSnapshot) -> str:
    telemetry = {str(item.get("execution_id")): item for item in value.telemetry}
    lines = [
        f"EXECUTION HISTORY — offset {value.execution_offset}",
        "LOGICAL ID              SIDE   STATE              SOURCE / PROVIDER",
    ]
    for item in value.executions:
        measured = telemetry.get(item.logical_execution_id, {})
        source = measured.get("intent_source") or "unavailable"
        provider = measured.get("provider_id") or "unavailable"
        lines.append(
            f"{_clip(item.logical_execution_id, 23):<23} "
            f"{item.side:<6} {_enum(item.state):<18} "
            f"{_clip(str(source), 16)} / {_clip(str(provider), 16)}"
        )
        lines.append(
            f"  attempt {item.submission_attempt}; signature "
            f"{item.signature or 'unavailable'}; position "
            f"{item.position_id or 'unavailable'}"
        )
        if item.error_classification is not None:
            lines.append(f"  error: {_enum(item.error_classification)}")
    if not value.executions:
        lines.append("No persisted executions in this page.")
    lines.append(
        "Ambiguous executions are inspection-only; the TUI never resubmits them."
    )
    return "\n".join(lines)


def _transaction(_value: TerminalSnapshot) -> str:
    return (
        "TRANSACTION LOOKUP\n\n"
        "Execution history displays full persisted signatures for bounded recent records.\n"
        "Interactive arbitrary lookup is not exposed because Hunter has no dedicated "
        "bounded application query for it."
    )


def _wallets(value: TerminalSnapshot) -> str:
    lines = [f"WALLETS & BALANCES — {value.wallet_freshness.value}", ""]
    for wallet in value.wallets:
        lines.extend(
            (
                f"Alias: {wallet.get('alias', 'unavailable')}",
                f"Public key: {wallet.get('public_key', 'unavailable')}",
                f"Balance (lamports): {_known(wallet.get('lamports'))}",
                f"Status: {wallet.get('status', 'unavailable')}",
                "",
            )
        )
    if not value.wallets:
        lines.append("unavailable (not fetched yet or provider read failed)")
    lines.append("Private keys and seed material are never displayed.")
    return "\n".join(lines)


def _tracked_wallets(value: TerminalSnapshot) -> str:
    lines = ["TRACKED WALLETS", ""]
    lines.extend(value.tracked_wallets or ("not configured",))
    if value.status.tracked_wallet is not None:
        lines.append(f"\nRuntime: {_enum(value.status.tracked_wallet.state)}")
    return "\n".join(lines)


def _wallet_fleet(value: TerminalSnapshot) -> str:
    capability = (
        "configured" if value.capabilities.get("wallet_fleet") else "not configured"
    )
    return (
        "WALLET FLEET\n\n"
        f"Capability: {capability}\n"
        f"Active fleets: {value.status.active_fleets}\n"
        "Fleet detail listing requires a plan ID and is not exposed as an unbounded "
        "operator query. Fleet trading controls remain outside M5."
    )


def _token_launches(value: TerminalSnapshot) -> str:
    capability = (
        "configured" if value.capabilities.get("token_launch") else "not configured"
    )
    return (
        "TOKEN LAUNCHES\n\n"
        f"Capability: {capability}\n"
        "Read-only launch history is unsupported by the current HunterApplication API.\n"
        "The TUI does not add launch construction or submission logic."
    )


def _risk(value: TerminalSnapshot) -> str:
    lines = ["RISK CONFIGURATION — READ ONLY", ""]
    for key, item in sorted(value.risk.items()):
        lines.append(f"{key}: {_render_value(item)}")
    if not value.risk:
        lines.append("unavailable")
    lines.append("\nRisk limits cannot be edited from the M5 terminal interface.")
    return "\n".join(lines)


def _events(value: TerminalSnapshot) -> str:
    lines = ["RECENT EVENTS & ERRORS", ""]
    for event in reversed(value.recent_events[-50:]):
        details = sanitize_text(event.attributes)
        lines.append(
            f"{event.occurred_at.isoformat()} {event.event_type:<28} {details}"
        )
    if not value.recent_events:
        lines.append("No events observed by the terminal adapter yet.")
    lines.extend(f"DATA ERROR: {sanitize_text(error)}" for error in value.data_errors)
    return "\n".join(lines)


def _latency(value: TerminalSnapshot) -> str:
    performance = value.performance
    lines = [
        f"LATENCY & LANDING — {performance.observations} persisted executions",
        f"Telemetry freshness: {value.telemetry_freshness.value}",
        "",
        "Metric                    p50             p95             p99",
    ]
    for key, readings in performance.percentiles.items():
        lines.append(
            f"{key:<25} {_display(readings['p50'], 'ms'):<15} "
            f"{_display(readings['p95'], 'ms'):<15} "
            f"{_display(readings['p99'], 'ms')}"
        )
    lines.extend(
        (
            "",
            "Landing from submission slot",
            f"  same slot: {_known(performance.same_slot)}",
            f"  next slot: {_known(performance.next_slot)}",
            f"  +2 or later: {_known(performance.later_slot)}",
            "No launch-relative block-0 claim is made without authoritative launch-slot data.",
        )
    )
    return "\n".join(lines)


def _settings(value: TerminalSnapshot) -> str:
    lines = ["SETTINGS & DIAGNOSTICS", ""]
    lines.extend(
        f"{key}: {_render_value(item)}" for key, item in value.settings.items()
    )
    if not value.settings:
        lines.append("No persisted non-secret settings.")
    lines.extend(
        (
            "",
            f"Last refresh: {value.refreshed_at.isoformat()}",
            f"Coalesced refresh requests: {value.refresh_coalesced}",
            "Authenticated endpoint URLs and credentials are intentionally omitted.",
        )
    )
    return "\n".join(lines) + _errors(value)


def _section(title: str, rows: tuple[tuple[str, str], ...]) -> str:
    lines = [title, ""]
    lines.extend(f"{label:<26} {item}" for label, item in rows)
    return "\n".join(lines)


def _display(value: DisplayValue, unit: str = "") -> str:
    if value.state == ValueState.AVAILABLE:
        return f"{value.value} {unit}".strip()
    if value.state == ValueState.INSUFFICIENT:
        return f"insufficient (n={value.samples})"
    return value.state.value


def _optional_ready(value: bool | None) -> str:  # noqa: FBT001
    if value is None:
        return "unavailable"
    return "ready" if value else "not ready"


def _known(value: object) -> str:
    return "unknown" if value is None else str(value)


def _enum(value: object) -> str:
    return str(getattr(value, "value", value))


def _yes_no(value: bool) -> str:  # noqa: FBT001
    return "yes" if value else "no"


def _clip(value: str, width: int) -> str:
    return value if len(value) <= width else value[: width - 1] + "…"


def _age(value: datetime) -> str:
    seconds = max(0.0, (datetime.now(UTC) - value).total_seconds())
    return f"{seconds:.1f}s ago"


def _render_value(value: object) -> str:
    if isinstance(value, dict):
        return sanitize_text(", ".join(f"{key}={item}" for key, item in value.items()))
    return sanitize_text(value)


def _errors(value: TerminalSnapshot) -> str:
    if not value.data_errors:
        return ""
    return "\n\nDEGRADED DATA\n" + "\n".join(
        f"  {sanitize_text(error)}" for error in value.data_errors
    )
