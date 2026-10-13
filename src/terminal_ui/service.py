"""Framework-neutral terminal lifecycle, snapshots, and safe controls."""

# UI-facing failures are deliberately concise and operator-actionable.
# ruff: noqa: BLE001, TRY003

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime
from statistics import median
from time import monotonic
from typing import TYPE_CHECKING, Any, TypeVar

from terminal_ui.events import TerminalEventBuffer
from terminal_ui.models import (
    DisplayValue,
    OperatorAction,
    OperatorConfirmation,
    PerformanceSnapshot,
    TerminalSnapshot,
    ValueState,
)
from utils.redaction import sanitize_text

if TYPE_CHECKING:
    from collections.abc import Callable

    from application.runtime_models import ApplicationEvent, RuntimeStatus
    from terminal_ui.config import TerminalUIConfig
    from terminal_ui.ports import TerminalApplication, TerminalDriver

_T = TypeVar("_T")
_SLOT_LATER_THRESHOLD = 2
_MINIMUM_MEDIAN_SAMPLES = 5


class UnsupportedTerminalControlError(ValueError):
    """A control has no safe application-level implementation."""


class TerminalConfirmationError(ValueError):
    """A confirmation was unknown, stale, or already consumed."""


class TerminalControlService:
    """Optional control interface isolated from Hunter's economic hot path."""

    name = "terminal_ui"

    def __init__(
        self,
        config: TerminalUIConfig,
        application: TerminalApplication,
        driver: TerminalDriver,
    ) -> None:
        self.config = config
        self.application = application
        self.driver = driver
        self._events = TerminalEventBuffer(
            config.event_queue_size, config.event_history_size
        )
        self._state_observer = lambda _state, _reason: None
        self._refresh_requested = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None
        self._driver_task: asyncio.Task[None] | None = None
        self._accepting = False
        self._stopping = False
        self._refresh_coalesced = 0
        self._position_offset = 0
        self._execution_offset = 0
        self._wallets: tuple[dict[str, object], ...] = ()
        self._wallets_updated_mono: float | None = None
        self._pending: dict[str, OperatorConfirmation] = {}

    def set_state_observer(self, observer: Callable[[str, str | None], None]) -> None:
        self._state_observer = observer

    async def start(self) -> None:
        """Subscribe and launch UI work without waiting for the UI to exit."""
        if self._driver_task is not None:
            return
        self._accepting = True
        self.application.event_bus.subscribe(self._consume_event)
        self._state_observer("starting", None)
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(), name=f"terminal-refresh:{self.application.bot_id}"
        )
        self._driver_task = asyncio.create_task(
            self.driver.start(
                request_action=self.stage_action,
                confirm_action=self.confirm_action,
                request_refresh=self.request_refresh,
                request_exit=self.stop_accepting,
                request_page=self.change_page,
            ),
            name=f"terminal-ui:{self.application.bot_id}",
        )
        self._driver_task.add_done_callback(self._driver_finished)
        self.request_refresh()
        ready = asyncio.create_task(self.driver.wait_ready())
        done, _pending = await asyncio.wait(
            {ready, self._driver_task},
            timeout=self.config.startup_timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if self._driver_task in done:
            ready.cancel()
            self._driver_task.result()
        if ready not in done:
            ready.cancel()
            self._state_observer("failed", "startup timeout")
            raise TimeoutError("terminal UI startup timed out")
        await ready
        self._state_observer("ready", None)

    def stop_accepting(self) -> None:
        """Stop terminal input without changing Hunter's runtime controls."""
        self._accepting = False
        self._refresh_requested.set()

    async def close(self) -> None:
        """Bound shutdown; never invoke HunterApplication.shutdown from the UI."""
        if self._stopping:
            return
        self._stopping = True
        self.stop_accepting()
        self._pending.clear()
        await self.driver.close()
        tasks = tuple(
            task
            for task in (self._refresh_task, self._driver_task)
            if task is not None and not task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=self.config.shutdown_timeout_seconds,
                )
            except TimeoutError:
                pass
        self._refresh_task = None
        self._driver_task = None
        self._state_observer("stopped", None)

    async def _consume_event(self, event: ApplicationEvent) -> None:
        self._events.publish_nowait(event)
        self.request_refresh()

    def request_refresh(self) -> None:
        """Coalesce redundant requests instead of growing a refresh queue."""
        if self._refresh_requested.is_set():
            self._refresh_coalesced += 1
        self._refresh_requested.set()

    async def refresh_once(self) -> TerminalSnapshot:
        """Read persisted/application state off the terminal rendering loop."""
        async with self._refresh_lock:
            errors: list[str] = []

            async def read(label: str, function: Callable[[], _T], default: _T) -> _T:
                try:
                    return await asyncio.to_thread(function)
                except Exception as error:
                    errors.append(f"{label}: {type(error).__name__}")
                    return default

            status = await asyncio.to_thread(self.application.runtime_status)
            positions = await read(
                "positions",
                lambda: self.application.list_positions_page(
                    limit=self.config.position_page_size,
                    offset=self._position_offset,
                ),
                [],
            )
            executions = await read(
                "executions",
                lambda: self.application.list_executions(
                    min(200, self.config.execution_page_size + self._execution_offset)
                ),
                [],
            )
            executions = executions[
                self._execution_offset : self._execution_offset
                + self.config.execution_page_size
            ]
            telemetry = await read(
                "telemetry",
                lambda: self.application.list_recent_telemetry(
                    self.config.telemetry_sample_size
                ),
                [],
            )
            risk = await read("risk", self.application.risk_settings, {})
            settings = await read("settings", self.application.get_settings, {})
            operator_settings = await read(
                "operator_settings", self.application.operator_settings, {}
            )
            tracked = await read(
                "tracked_wallets", self.application.list_tracked_wallets, ()
            )
            await self._refresh_wallets(errors)
            execution = self.application.config.get("execution") or {}
            routing_mode = str(
                execution.get("mode", execution.get("routing_mode", "single"))
            )
            snapshot = TerminalSnapshot(
                status=status,
                positions=tuple(positions),
                executions=tuple(executions),
                wallets=self._wallets,
                tracked_wallets=tuple(tracked),
                risk=risk,
                settings={
                    **_safe_settings(settings),
                    **_safe_settings(operator_settings),
                },
                recent_events=self._events.drain(),
                telemetry=tuple(telemetry),
                performance=performance_snapshot(telemetry),
                telemetry_freshness=_telemetry_freshness(
                    telemetry, self.config.stale_after_seconds
                ),
                wallet_freshness=self._wallet_freshness(),
                capabilities={
                    "tracked_wallets": (
                        getattr(
                            self.application.features, "tracked_wallet_service", None
                        )
                        is not None
                    ),
                    "wallet_fleet": (
                        getattr(self.application.features, "wallet_fleet_service", None)
                        is not None
                    ),
                    "token_launch": (
                        getattr(self.application.features, "token_launch_service", None)
                        is not None
                    ),
                },
                routing_mode=routing_mode,
                position_offset=self._position_offset,
                execution_offset=self._execution_offset,
                event_drops=self._events.dropped,
                refresh_coalesced=self._refresh_coalesced,
                data_errors=tuple(errors),
            )
            self.driver.publish_snapshot(snapshot)
            return snapshot

    async def _refresh_wallets(self, errors: list[str]) -> None:
        now = monotonic()
        if (
            self._wallets_updated_mono is not None
            and now - self._wallets_updated_mono < self.config.balance_refresh_seconds
        ):
            return
        try:
            self._wallets = await asyncio.wait_for(
                self.application.wallet_balances(),
                timeout=min(self.config.balance_refresh_seconds, 10.0),
            )
            self._wallets_updated_mono = now
        except Exception as error:
            errors.append(f"wallets: {type(error).__name__}")

    def _wallet_freshness(self) -> ValueState:
        if self._wallets_updated_mono is None:
            return ValueState.UNAVAILABLE
        age = monotonic() - self._wallets_updated_mono
        if age > self.config.stale_after_seconds:
            return ValueState.STALE
        return ValueState.AVAILABLE

    async def _refresh_loop(self) -> None:
        while self._accepting:
            try:
                await asyncio.wait_for(
                    self._refresh_requested.wait(),
                    timeout=self.config.refresh_interval_seconds,
                )
            except TimeoutError:
                pass
            self._refresh_requested.clear()
            if not self._accepting:
                return
            try:
                await self.refresh_once()
            except Exception as error:
                self._state_observer("degraded", type(error).__name__)

    async def stage_action(self, action: OperatorAction) -> OperatorConfirmation:
        """Create an opaque confirmation after inspecting current runtime state."""
        if not self._accepting:
            raise TerminalConfirmationError("terminal is not accepting actions")
        status = await asyncio.to_thread(self.application.runtime_status)
        summary = _action_summary(action, status)
        now = monotonic()
        confirmation = OperatorConfirmation(
            nonce=secrets.token_urlsafe(12),
            action=action,
            summary=summary,
            created_mono=now,
            expires_mono=now + 20.0,
        )
        self._pending.clear()
        self._pending[confirmation.nonce] = confirmation
        return confirmation

    async def confirm_action(self, nonce: str) -> None:
        """Atomically consume and revalidate a control at execution time."""
        confirmation = self._pending.pop(nonce, None)
        if confirmation is None:
            raise TerminalConfirmationError("unknown or already used confirmation")
        if monotonic() >= confirmation.expires_mono:
            raise TerminalConfirmationError("confirmation expired")
        status = await asyncio.to_thread(self.application.runtime_status)
        action = confirmation.action
        if action == OperatorAction.ENABLE_TRADING:
            if status.kill_switch:
                raise TerminalConfirmationError(
                    "kill switch is active; trading cannot be enabled"
                )
            await asyncio.to_thread(self.application.enable_trading)
        elif action == OperatorAction.DISABLE_TRADING:
            await asyncio.to_thread(self.application.disable_trading)
        elif action == OperatorAction.ACTIVATE_KILL_SWITCH:
            await asyncio.to_thread(self.application.activate_kill_switch)
        elif action == OperatorAction.CLEAR_KILL_SWITCH:
            raise UnsupportedTerminalControlError(
                "Hunter has no safe application API for clearing the kill switch"
            )
        self.request_refresh()

    def set_page(self, view: str, offset: int) -> None:
        """Set bounded table offsets without querying an entire history."""
        value = max(0, offset)
        if view == "positions":
            self._position_offset = value
        elif view == "executions":
            self._execution_offset = value
        else:
            raise ValueError("unsupported paginated view")
        self.request_refresh()

    def change_page(self, view: str, direction: int) -> None:
        """Move one bounded page forward or backward for supported histories."""
        if direction not in {-1, 1}:
            raise ValueError("page direction must be -1 or 1")
        if view == "positions":
            self.set_page(
                view,
                self._position_offset + direction * self.config.position_page_size,
            )
        elif view == "executions":
            self.set_page(
                view,
                self._execution_offset + direction * self.config.execution_page_size,
            )
        else:
            raise ValueError("current view is not paginated")

    def _driver_finished(self, task: asyncio.Task[None]) -> None:
        if self._stopping:
            return
        self._accepting = False
        self._refresh_requested.set()
        if task.cancelled():
            self._state_observer("stopped", None)
            return
        error = task.exception()
        self._state_observer(
            "failed" if error is not None else "stopped",
            type(error).__name__ if error is not None else None,
        )


def performance_snapshot(rows: list[dict[str, Any]]) -> PerformanceSnapshot:
    """Summarize actual persisted monotonic measurements only."""
    detection = _durations(rows, "detected_mono_ns", "trade_requested_mono_ns")
    build = _durations(rows, "build_started_mono_ns", "build_completed_mono_ns")
    sign = _durations(rows, "signing_started_mono_ns", "signing_completed_mono_ns")
    submit = _durations(rows, "submission_started_mono_ns", "rpc_responded_mono_ns")
    acknowledged = _provider_rtts(rows)
    landing = _durations(rows, "submission_started_mono_ns", "processed_mono_ns")
    deltas = [
        int(row["landed_slot"]) - int(row["submitted_slot"])
        for row in rows
        if row.get("landed_slot") is not None
        and row.get("submitted_slot") is not None
        and int(row["landed_slot"]) >= int(row["submitted_slot"])
    ]
    return PerformanceSnapshot(
        detection_to_decision_ms=_latency_value(detection),
        build_ms=_latency_value(build),
        sign_ms=_latency_value(sign),
        submit_ms=_latency_value(submit),
        acknowledgement_ms=_latency_value(acknowledged),
        landing_ms=_latency_value(landing),
        same_slot=sum(value == 0 for value in deltas) if deltas else None,
        next_slot=sum(value == 1 for value in deltas) if deltas else None,
        later_slot=(
            sum(value >= _SLOT_LATER_THRESHOLD for value in deltas) if deltas else None
        ),
        observations=len(rows),
        percentiles={
            "detection_to_decision": _percentiles(detection),
            "build": _percentiles(build),
            "sign": _percentiles(sign),
            "submission": _percentiles(submit),
            "acknowledgement": _percentiles(acknowledged),
            "landing": _percentiles(landing),
        },
    )


def percentile(values: list[float], quantile: float) -> float | None:
    """Return a nearest-rank percentile for already measured samples."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * quantile) - 1))
    return ordered[index]


def _latency_value(values: list[float]) -> DisplayValue:
    if not values:
        return DisplayValue(None, ValueState.UNMEASURED)
    state = (
        ValueState.AVAILABLE
        if len(values) >= _MINIMUM_MEDIAN_SAMPLES
        else ValueState.INSUFFICIENT
    )
    return DisplayValue(round(median(values), 3), state, len(values))


def _percentiles(values: list[float]) -> dict[str, DisplayValue]:
    results: dict[str, DisplayValue] = {}
    for name, quantile, minimum in (
        ("p50", 0.50, 5),
        ("p95", 0.95, 20),
        ("p99", 0.99, 100),
    ):
        if not values:
            results[name] = DisplayValue(None, ValueState.UNMEASURED)
        elif len(values) < minimum:
            results[name] = DisplayValue(None, ValueState.INSUFFICIENT, len(values))
        else:
            results[name] = DisplayValue(
                round(percentile(values, quantile) or 0.0, 3),
                ValueState.AVAILABLE,
                len(values),
            )
    return results


def _durations(rows: list[dict[str, Any]], start: str, end: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        first = row.get(start)
        second = row.get(end)
        if (
            isinstance(first, int | float)
            and isinstance(second, int | float)
            and second >= first
        ):
            values.append((second - first) / 1_000_000)
    return values


def _provider_rtts(rows: list[dict[str, Any]]) -> list[float]:
    values: list[float] = []
    for row in rows:
        for attempt in row.get("provider_attempts") or []:
            started = attempt.get("submit_started_mono_ns")
            ended = attempt.get("acknowledged_mono_ns")
            if (
                isinstance(started, int | float)
                and isinstance(ended, int | float)
                and ended >= started
            ):
                values.append((ended - started) / 1_000_000)
    return values


def _action_summary(action: OperatorAction, status: RuntimeStatus) -> str:
    if action == OperatorAction.ENABLE_TRADING:
        return (
            "Enable new exposure. Existing risk limits and final authorization "
            "remain active; this does not clear the kill switch."
        )
    if action == OperatorAction.DISABLE_TRADING:
        return (
            "Block new exposure. Managed defensive exits and runtime monitoring "
            "remain available."
        )
    if action == OperatorAction.ACTIVATE_KILL_SWITCH:
        return (
            "Immediately block new exposure. This does not liquidate positions, "
            "cancel submitted transactions, or block eligible defensive exits."
        )
    return (
        "Request kill-switch clearing. Current state: "
        f"{'active' if status.kill_switch else 'inactive'}. Hunter currently "
        "exposes no safe clear operation, so confirmation will fail closed."
    )


def _safe_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Exclude credential-shaped keys even if persisted settings grow later."""
    blocked = ("key", "secret", "token", "password", "endpoint", "url")
    return {
        str(key): _safe_setting_value(value)
        for key, value in settings.items()
        if not any(part in str(key).lower() for part in blocked)
    }


def _safe_setting_value(value: object) -> object:
    if isinstance(value, dict):
        return _safe_settings(value)
    if isinstance(value, list | tuple):
        return tuple(_safe_setting_value(item) for item in value)
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def _telemetry_freshness(
    rows: list[dict[str, Any]], stale_after_seconds: float
) -> ValueState:
    timestamps: list[datetime] = []
    for row in rows:
        raw = row.get("processed_at") or row.get("confirmed_at")
        if not isinstance(raw, str):
            continue
        try:
            timestamps.append(datetime.fromisoformat(raw))
        except ValueError:
            continue
    if not timestamps:
        return ValueState.UNAVAILABLE if not rows else ValueState.UNMEASURED
    latest = max(timestamps)
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - latest.astimezone(UTC)).total_seconds()
    return ValueState.STALE if age > stale_after_seconds else ValueState.AVAILABLE
