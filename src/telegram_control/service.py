"""Secure private-owner Telegram commands over Hunter application APIs."""

# Command syntax errors are deliberately concise and user-facing.
# ruff: noqa: ANN401, C901, PLR0911, PLR2004, TC001, TRY003

from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import Awaitable, Callable, Sequence
from time import monotonic
from typing import Any

from domain.amounts import BasisPoints
from domain.intents import TradeIntent
from domain.launch import FleetExecutionPolicy, FleetExitType
from execution.errors import ErrorClassification, ExecutionError
from telegram_control.config import TelegramConfig
from telegram_control.models import (
    IncomingCallback,
    IncomingMessage,
    LaunchPendingPayload,
    MessageButton,
    NotificationPriority,
    PendingAction,
    PendingActionState,
    PendingActionType,
    TelegramMetrics,
    TelegramNotification,
    TelegramServiceState,
)
from telegram_control.notifications import TelegramNotificationQueue
from telegram_control.pending import PendingActionError, PendingActionStore
from telegram_control.ports import TelegramApplication, TelegramTransport
from telegram_control.rate_limit import OwnerRateLimiter
from telegram_control.views import (
    decimal_text,
    execution_text,
    position_text,
    safe_settings_text,
    split_message,
    status_text,
)
from utils.redaction import sanitize_text

LaunchResolver = Callable[
    [tuple[str, ...]],
    Awaitable[tuple[object, dict[str, object]]] | tuple[object, dict[str, object]],
]
StateObserver = Callable[[str, str | None], None]

_HELP = """Hunter commands

Runtime: /status /enable /disable /kill
Trading: /balance /buy /sell
Positions: /positions /position /pnl
Execution: /executions /tx
Infrastructure: /providers /feeds /infra /settings
Tracking: /tracked /wallets
Fleet: /fleet
Launch: /launch
Risk: /risk

Economic actions use short-lived server-side confirmation."""

_EVENTS: dict[str, tuple[NotificationPriority, str, str]] = {
    "RuntimeStarted": (NotificationPriority.NORMAL, "runtime", "Runtime started"),
    "RuntimeReady": (NotificationPriority.NORMAL, "runtime", "Runtime ready"),
    "RuntimeDegraded": (NotificationPriority.HIGH, "runtime", "Runtime degraded"),
    "RuntimeFailed": (NotificationPriority.CRITICAL, "runtime", "Runtime failed"),
    "RuntimeShuttingDown": (
        NotificationPriority.NORMAL,
        "runtime",
        "Runtime shutting down",
    ),
    "TradingEnabled": (NotificationPriority.HIGH, "control", "Trading enabled"),
    "TradingDisabled": (NotificationPriority.HIGH, "control", "Trading disabled"),
    "KillSwitchActivated": (
        NotificationPriority.CRITICAL,
        "control",
        "Kill switch activated: new exposure blocked",
    ),
    "PositionOpened": (NotificationPriority.NORMAL, "position", "Position opened"),
    "PositionClosed": (NotificationPriority.NORMAL, "position", "Position closed"),
    "TakeProfitTriggered": (NotificationPriority.NORMAL, "position", "TP triggered"),
    "StopLossTriggered": (NotificationPriority.HIGH, "position", "SL triggered"),
    "TimedExitTriggered": (NotificationPriority.NORMAL, "position", "Timed exit"),
    "EmergencyExitTriggered": (
        NotificationPriority.HIGH,
        "position",
        "Emergency exit",
    ),
    "ExecutionSubmitted": (
        NotificationPriority.NORMAL,
        "execution",
        "Execution submitted",
    ),
    "ExecutionConfirmed": (
        NotificationPriority.NORMAL,
        "execution",
        "Execution confirmed",
    ),
    "ExecutionFailed": (
        NotificationPriority.HIGH,
        "execution",
        "Execution failed",
    ),
    "ExecutionAmbiguous": (
        NotificationPriority.HIGH,
        "execution",
        "Execution outcome ambiguous",
    ),
    "ProviderDegraded": (
        NotificationPriority.HIGH,
        "provider",
        "Provider degraded",
    ),
    "FeedDegraded": (NotificationPriority.HIGH, "feed", "Feed degraded"),
    "RecoveryIssue": (
        NotificationPriority.CRITICAL,
        "recovery",
        "Recovery requires reconciliation",
    ),
    "TrackedWalletCreate": (
        NotificationPriority.NORMAL,
        "tracked",
        "Tracked CREATE observed",
    ),
    "TrackedWalletBuy": (
        NotificationPriority.NORMAL,
        "tracked",
        "Tracked BUY observed",
    ),
    "LaunchSubmitted": (
        NotificationPriority.NORMAL,
        "launch",
        "Launch submitted",
    ),
    "BundleLanded": (NotificationPriority.NORMAL, "bundle", "Bundle landed"),
    "BundleFailed": (NotificationPriority.HIGH, "bundle", "Bundle failed"),
    "BundleAmbiguous": (
        NotificationPriority.HIGH,
        "bundle",
        "Bundle outcome ambiguous",
    ),
    "FleetExitStatus": (NotificationPriority.NORMAL, "fleet", "Fleet exit update"),
    "ControlInterfaceDegraded": (
        NotificationPriority.HIGH,
        "runtime",
        "Control interface degraded",
    ),
}


class TelegramControlService:
    """Thin asynchronous command and notification adapter."""

    name = "telegram"

    def __init__(
        self,
        config: TelegramConfig,
        application: TelegramApplication,
        transport: TelegramTransport,
        *,
        launch_resolver: LaunchResolver | None = None,
    ) -> None:
        if not config.enabled:
            raise ValueError("disabled Telegram must not be composed")
        self.config = config
        self.application = application
        self.transport = transport
        self.launch_resolver = launch_resolver
        self.state = TelegramServiceState.STOPPED
        self.metrics = TelegramMetrics()
        self.pending = PendingActionStore(config.maximum_pending_actions)
        self.rate_limiter = OwnerRateLimiter(
            config.command_limit, config.command_interval_seconds
        )
        owner_chats = tuple(sorted(config.owner_user_ids))
        self.notifications = TelegramNotificationQueue(
            transport,
            owner_chats,
            self.metrics,
            maximum_pending=config.notification_queue_size,
        )
        self._state_observer: StateObserver | None = None
        self._accepting_commands = False
        self.application.event_bus.subscribe(self.handle_application_event)

    def set_state_observer(self, observer: StateObserver) -> None:
        self._state_observer = observer

    async def start(self) -> None:
        self._set_state(TelegramServiceState.STARTING)
        try:
            await asyncio.wait_for(
                self.transport.start(
                    self.handle_message,
                    self.handle_callback,
                    self.transport_failed,
                ),
                timeout=self.config.startup_timeout_seconds,
            )
            await self.notifications.start()
        except Exception:
            self._set_state(TelegramServiceState.FAILED, "transport startup failed")
            raise
        self._accepting_commands = True
        self._set_state(TelegramServiceState.CONNECTED)

    def stop_accepting(self) -> None:
        self._accepting_commands = False
        self.pending.cancel_all()

    async def close(self) -> None:
        self.stop_accepting()
        await self.notifications.close(
            flush_timeout_seconds=self.config.shutdown_timeout_seconds
        )
        try:
            await asyncio.wait_for(
                self.transport.close(), timeout=self.config.shutdown_timeout_seconds
            )
        except Exception:  # noqa: BLE001
            self.metrics.telegram_errors += 1
        self._set_state(TelegramServiceState.STOPPED)

    async def handle_message(self, message: IncomingMessage) -> None:
        started = monotonic()
        self.metrics.commands_received += 1
        command = "unknown"
        result = "rejected"
        try:
            if not self._authorized(message.user_id, message.chat_type):
                self.metrics.unauthorized_attempts += 1
                await self._send(message.chat_id, "This bot is not available.")
                return
            if not self._accepting_commands:
                self.metrics.commands_rejected += 1
                await self._send(message.chat_id, "Hunter control is shutting down.")
                return
            command, args = _parse_command(message.text)
            if not self.rate_limiter.allow(message.user_id, exempt=command == "kill"):
                self.metrics.commands_rejected += 1
                await self._send(
                    message.chat_id, "Command rate limit reached. Try again shortly."
                )
                return
            await self._dispatch(command, args, message)
            result = "accepted"
        except Exception as error:  # noqa: BLE001
            self.metrics.commands_rejected += 1
            await self._send(message.chat_id, _safe_error(error))
        finally:
            logging.getLogger(__name__).info(
                "Telegram command=%s user_id=%s chat_id=%s result=%s latency_ms=%.2f",
                command,
                message.user_id,
                message.chat_id,
                result,
                (monotonic() - started) * 1000,
            )

    async def handle_callback(self, callback: IncomingCallback) -> None:
        self.metrics.callbacks_received += 1
        if not self._authorized(callback.user_id, callback.chat_type):
            self.metrics.unauthorized_attempts += 1
            await self.transport.answer_callback(callback.callback_id, "Not authorized")
            return
        if not self._accepting_commands:
            await self.transport.answer_callback(
                callback.callback_id, "Hunter is shutting down"
            )
            return
        try:
            operation, nonce = _parse_callback(callback.data)
            if operation == "cancel":
                self.pending.cancel(
                    nonce,
                    owner_user_id=callback.user_id,
                    chat_id=callback.chat_id,
                )
                await self.transport.answer_callback(callback.callback_id, "Cancelled")
                await self._send(callback.chat_id, "Action cancelled.")
                return
            action = self.pending.claim(
                nonce,
                owner_user_id=callback.user_id,
                chat_id=callback.chat_id,
            )
        except PendingActionError as error:
            if error.code == "expired":
                self.metrics.pending_expired += 1
            if error.code == "replayed":
                self.metrics.callbacks_replayed += 1
            await self.transport.answer_callback(
                callback.callback_id, _pending_error_text(error.code)
            )
            return
        except ValueError:
            await self.transport.answer_callback(callback.callback_id, "Invalid action")
            return
        await self.transport.answer_callback(callback.callback_id, "Processing")
        try:
            result = await self._execute_pending(action)
        except Exception as error:  # noqa: BLE001
            self.pending.finish(action.nonce, PendingActionState.FAILED)
            await self._send(callback.chat_id, _safe_error(error))
        else:
            self.pending.finish(action.nonce, PendingActionState.CONFIRMED)
            await self._send(callback.chat_id, result)

    async def handle_application_event(self, event: Any) -> None:
        """Convert one event to a queue insertion; never send inline."""
        definition = _EVENTS.get(event.event_type)
        if definition is None:
            return
        priority, category, title = definition
        categories = self.config.notification_categories
        if "all" not in categories and category not in categories:
            return
        attributes = event.attributes
        detail = " ".join(
            f"{key}={sanitize_text(value)}"
            for key, value in attributes.items()
            if key in {"mint", "position_id", "signature", "source", "reason"}
            and value is not None
        )
        identity = next(
            (
                str(attributes[key])
                for key in ("intent_id", "execution_id", "position_id", "signature")
                if attributes.get(key)
            ),
            None,
        )
        self.notifications.publish_nowait(
            TelegramNotification(
                f"{title}{': ' + detail if detail else ''}",
                priority,
                category,
                f"{event.event_type}:{identity}" if identity else None,
            )
        )

    def transport_failed(self, error: BaseException) -> None:
        self.metrics.telegram_errors += 1
        self._set_state(TelegramServiceState.DEGRADED, type(error).__name__)

    async def _dispatch(
        self, command: str, args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        handlers = {
            "start": self._start_command,
            "help": self._help_command,
            "status": self._status_command,
            "balance": self._balance_command,
            "wallets": self._wallets_command,
            "positions": self._positions_command,
            "position": self._position_command,
            "pnl": self._pnl_command,
            "buy": self._buy_command,
            "sell": self._sell_command,
            "executions": self._executions_command,
            "tx": self._tx_command,
            "risk": self._risk_command,
            "enable": self._enable_command,
            "disable": self._disable_command,
            "kill": self._kill_command,
            "providers": self._providers_command,
            "feeds": self._feeds_command,
            "infra": self._infra_command,
            "settings": self._settings_command,
            "tracked": self._tracked_command,
            "fleet": self._fleet_command,
            "launch": self._launch_command,
        }
        handler = handlers.get(command)
        if handler is None:
            raise ValueError("Unknown command. Use /help.")
        await handler(args, message)

    async def _start_command(
        self, _args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        status = self.application.runtime_status()
        text = (
            "Hunter\n"
            f"Runtime: {status.application_state.value}\n"
            f"Trading: {'enabled' if status.trading_enabled else 'disabled'}\n"
            f"Kill switch: {'active' if status.kill_switch else 'inactive'}\n\n"
            "Use /help for runtime, trading, positions, execution, infrastructure, "
            "tracking, fleet, launch, and risk commands."
        )
        await self._send(message.chat_id, text)

    async def _help_command(
        self, _args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        await self._send(message.chat_id, _HELP)

    async def _status_command(
        self, _args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        mode = str(
            self.application.config.get("trade", {}).get("exit_strategy", "configured")
        )
        await self._send(
            message.chat_id, status_text(self.application.runtime_status(), mode)
        )

    async def _balance_command(
        self, _args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        balances = await self.application.wallet_balances()
        lines = ["Managed wallet balances"]
        total = 0
        for item in balances:
            lamports = int(item["lamports"])
            total += lamports
            lines.append(
                f"{item['alias']}: {decimal_text(lamports, 9)} SOL ({item['public_key']})"
            )
        lines.insert(1, f"Total: {decimal_text(total, 9)} SOL")
        await self._send(message.chat_id, "\n".join(lines))

    async def _wallets_command(
        self, args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        await self._balance_command(args, message)

    async def _positions_command(
        self, _args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        positions = [
            item
            for item in self.application.list_positions()
            if item.accounting.status.value != "closed"
        ]
        if not positions:
            await self._send(message.chat_id, "No active positions.")
            return
        await self._send(
            message.chat_id,
            "Active positions\n\n"
            + "\n\n".join(position_text(item) for item in positions),
        )

    async def _position_command(
        self, args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        if len(args) != 1:
            raise ValueError("Usage: /position <position-id|mint>")
        position = self._find_position(args[0])
        await self._send(message.chat_id, position_text(position, detailed=True))

    async def _pnl_command(
        self, args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        positions = (
            (self._find_position(args[0]),)
            if args
            else tuple(self.application.list_positions())
        )
        if not positions:
            await self._send(message.chat_id, "No position accounting available.")
            return
        lines = ["Execution-based realized PnL"]
        for position in positions:
            values = self.application.get_realized_pnl(position.accounting.position_id)
            lines.extend(
                (
                    "",
                    f"Position: {position.accounting.position_id}",
                    f"Quote: {position.accounting.quote_mint}",
                    f"Gross raw: {values['gross_raw']}",
                    f"Net raw: {values['net_raw'] if values['net_raw'] is not None else 'unknown'}",
                    f"Remaining cost basis raw: {values['remaining_cost_basis_raw']}",
                    f"Network fees (lamports): {position.accounting.entry_network_fee_lamports} entry / {position.accounting.exit_network_fee_lamports} exit",
                    f"Priority fees (lamports): {position.accounting.entry_priority_fee_lamports} entry / {position.accounting.exit_priority_fee_lamports} exit",
                )
            )
        await self._send(message.chat_id, "\n".join(lines))

    async def _buy_command(
        self, args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        if len(args) not in {2, 3}:
            raise ValueError("Usage: /buy <mint> <amount> [quote-mint|SOL|USDC]")
        intent = self.application.prepare_manual_buy(
            args[0], args[1], args[2] if len(args) == 3 else None
        )
        preview = self.application.preview_manual_trade(intent)
        summary = "\n".join(
            (
                "Confirm manual buy",
                f"Mint: {intent.mint}",
                f"Amount raw: {intent.quote_amount.value}",
                f"Quote: {intent.quote_mint}",
                f"Wallet: {intent.wallet_id}",
                f"Slippage: {intent.slippage.value} bps",
                f"Execution: {preview['execution_mode']}",
                f"Risk: {preview['risk']}",
                "Output is an estimate, never guaranteed.",
            )
        )
        await self._request_confirmation(
            message, PendingActionType.BUY, intent, summary
        )

    async def _sell_command(
        self, args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        if len(args) != 2:
            raise ValueError("Usage: /sell <position-id> <percentage%>")
        intent = self.application.prepare_manual_sell(args[0], args[1])
        preview = self.application.preview_manual_trade(intent)
        summary = "\n".join(
            (
                "Confirm managed sell",
                f"Position: {intent.position_id}",
                f"Mint: {intent.mint}",
                f"Token amount raw: {intent.token_amount.value}",
                f"Slippage: {intent.slippage.value} bps",
                f"Risk: {preview['risk']}",
            )
        )
        await self._request_confirmation(
            message, PendingActionType.SELL, intent, summary
        )

    async def _executions_command(
        self, args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        limit = int(args[0]) if args else 10
        executions = self.application.list_executions(limit)
        if not executions:
            await self._send(message.chat_id, "No managed executions.")
            return
        await self._send(
            message.chat_id,
            "Recent executions\n\n"
            + "\n\n".join(execution_text(item) for item in executions),
        )

    async def _tx_command(
        self, args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        if len(args) != 1:
            raise ValueError("Usage: /tx <execution-id|signature>")
        await self._send(
            message.chat_id, execution_text(self.application.get_execution(args[0]))
        )

    async def _risk_command(
        self, _args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        await self._send(
            message.chat_id, safe_settings_text(self.application.risk_settings())
        )

    async def _enable_command(
        self, args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        if args:
            raise ValueError("Usage: /enable")
        status = self.application.runtime_status()
        await self._request_confirmation(
            message,
            PendingActionType.ENABLE,
            None,
            "Enable new exposure?\n"
            f"Current trading: {status.trading_enabled}\n"
            f"Kill switch: {status.kill_switch}\n"
            "This does not clear an active kill switch.",
        )

    async def _disable_command(
        self, args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        if args:
            raise ValueError("Usage: /disable")
        await self._request_confirmation(
            message,
            PendingActionType.DISABLE,
            None,
            "Disable new exposure? Defensive exits remain available.",
        )

    async def _kill_command(
        self, args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        if args == ("confirm",):
            self.application.activate_kill_switch()
            await self._send(
                message.chat_id,
                "Kill switch active. New exposure is blocked; managed defensive exits remain available.",
            )
            return
        if args:
            raise ValueError("Usage: /kill or /kill confirm")
        await self._request_confirmation(
            message,
            PendingActionType.KILL,
            None,
            "Activate kill switch now?\nNew exposure stops immediately. No automatic liquidation occurs.",
            ttl_seconds=self.config.kill_confirmation_ttl_seconds,
        )

    async def _providers_command(
        self, _args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        providers = self.application.runtime_status().providers
        text = "Providers\n" + (
            "\n".join(
                f"{item.name}: {item.state.value}{' (' + item.reason + ')' if item.reason else ''}"
                for item in providers
            )
            if providers
            else "No provider status available."
        )
        await self._send(message.chat_id, text)

    async def _feeds_command(
        self, _args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        feeds = self.application.runtime_status().listeners
        text = "Detection feeds\n" + "\n".join(
            f"{item.name}: {item.state.value}{' (' + item.reason + ')' if item.reason else ''}"
            for item in feeds
        )
        await self._send(message.chat_id, text)

    async def _infra_command(
        self, _args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        status = self.application.runtime_status()
        metrics = self.metrics.snapshot(self.pending.active_count)
        controls = (
            ", ".join(
                f"{item.name}={item.state.value}" for item in status.control_interfaces
            )
            or "none"
        )
        text = "\n".join(
            (
                "Infrastructure",
                f"Database ready: {status.database_ready}",
                f"Blockhash cache ready: {status.blockhash_ready}",
                f"Priority-fee cache ready: {status.fee_cache_ready}",
                f"Jito-tip cache ready: {status.tip_cache_ready}",
                f"Application event drops: {status.dropped_events}",
                f"Pending executions: {status.pending_executions}",
                f"Reconciliation required: {status.pending_reconciliation}",
                f"Control interfaces: {controls}",
                f"Telegram pending actions: {metrics['pending_actions']}",
                f"Telegram notification drops: {metrics['notifications_dropped']}",
                f"Telegram errors: {metrics['telegram_errors']}",
            )
        )
        await self._send(message.chat_id, text)

    async def _settings_command(
        self, _args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        values = {
            **self.application.get_settings(),
            "platform": self.application.config.get("platform", "pump_fun"),
            "listener_type": self.application.config.get("filters", {}).get(
                "listener_type", "unknown"
            ),
            "telegram_enabled": True,
            "telegram_private_chat_only": self.config.private_chat_only,
            "telegram_confirmation_ttl_seconds": self.config.confirmation_ttl_seconds,
        }
        await self._send(message.chat_id, safe_settings_text(values))

    async def _tracked_command(
        self, args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        if args:
            raise ValueError(
                "Tracked-wallet configuration is read-only in Milestone 4."
            )
        wallets = self.application.list_tracked_wallets()
        state = self.application.runtime_status().tracked_wallet
        text = "Tracked wallets\n" + (
            "\n".join(wallets) if wallets else "None configured."
        )
        if state is not None:
            text += f"\nRuntime: {state.state.value}"
        await self._send(message.chat_id, text)

    async def _fleet_command(
        self, args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        if not args:
            status = self.application.runtime_status()
            await self._send(
                message.chat_id,
                f"Wallet fleet\nActive fleets: {status.active_fleets}\n"
                "Use /fleet <plan-id> or /fleet exit <plan-id> <trigger> <mode> <slippage-bps>.",
            )
            return
        if args[0] != "exit":
            positions = self.application.fleet_positions(args[0])
            await self._send(
                message.chat_id,
                f"Fleet {args[0]}\nPositions: {len(positions)}\n"
                + "\n".join(
                    f"{item['fleet_position_id']}: {item['status']} raw={item['remaining_quantity_raw']}"
                    for item in positions
                ),
            )
            return
        if len(args) != 5:
            raise ValueError(
                "Usage: /fleet exit <plan-id> <manual|emergency|stop_loss|take_profit|time_based|profit_target> <bundle|parallel_fast|sequential> <slippage-bps>"
            )
        plan_id = args[1]
        positions = self.application.fleet_positions(plan_id)
        if not positions:
            raise ValueError("No eligible managed fleet positions found.")
        payload = {
            "plan_id": plan_id,
            "trigger": FleetExitType(args[2]),
            "policy": FleetExecutionPolicy(args[3]),
            "slippage": BasisPoints(int(args[4])),
        }
        await self._request_confirmation(
            message,
            PendingActionType.FLEET_EXIT,
            payload,
            f"Confirm fleet exit\nPlan: {plan_id}\nPositions: {len(positions)}\nTrigger: {args[2]}\nMode: {args[3]}\nSlippage: {args[4]} bps",
        )

    async def _launch_command(
        self, args: tuple[str, ...], message: IncomingMessage
    ) -> None:
        if self.application.features.token_launch_service is None:
            raise ValueError("Token launch capability is not configured.")
        if self.launch_resolver is None:
            raise ValueError(
                "Telegram launch resolver is not configured; typed launch requests remain available through the application API."
            )
        resolved = self.launch_resolver(args)
        request, submission_values = (
            await resolved if asyncio.iscoroutine(resolved) else resolved
        )
        plan = self.application.prepare_token_launch(request)
        components = getattr(plan, "components", ())
        summary = "\n".join(
            (
                "Confirm token launch",
                f"Plan: {plan.plan_id}",
                f"Mint: {plan.mint}",
                f"Execution: {plan.execution_policy.value}",
                f"Components: {len(components)}",
                f"Capital raw: {sum(item.quote_amount_raw or 0 for item in components)}",
                "Risk and bundle capacity revalidate before submission.",
            )
        )
        await self._request_confirmation(
            message,
            PendingActionType.LAUNCH,
            LaunchPendingPayload(request, submission_values, plan.plan_id),
            summary,
        )

    async def _request_confirmation(
        self,
        message: IncomingMessage,
        action_type: PendingActionType,
        payload: object,
        summary: str,
        *,
        ttl_seconds: float | None = None,
    ) -> None:
        action = self.pending.create(
            owner_user_id=message.user_id,
            chat_id=message.chat_id,
            action_type=action_type,
            payload=payload,
            summary=summary,
            ttl_seconds=ttl_seconds or self.config.confirmation_ttl_seconds,
        )
        await self._send(
            message.chat_id,
            f"{summary}\n\nExpires in {int(ttl_seconds or self.config.confirmation_ttl_seconds)} seconds.",
            (
                MessageButton("Confirm", f"confirm:{action.nonce}"),
                MessageButton("Cancel", f"cancel:{action.nonce}"),
            ),
        )

    async def _execute_pending(self, action: PendingAction) -> str:
        if action.action_type in {PendingActionType.BUY, PendingActionType.SELL}:
            intent = action.payload
            if not isinstance(intent, TradeIntent):
                raise ValueError("Malformed pending trade intent.")
            self.application.preview_manual_trade(intent)
            result = await self.application.submit_manual_trade(intent)
            signature = getattr(result, "signature", None)
            return f"{action.action_type.value.title()} accepted. Signature: {signature or 'pending observation'}"
        if action.action_type == PendingActionType.ENABLE:
            self.application.enable_trading()
            return "Trading enabled."
        if action.action_type == PendingActionType.DISABLE:
            self.application.disable_trading()
            return "New exposure disabled. Managed defensive exits remain available."
        if action.action_type == PendingActionType.KILL:
            self.application.activate_kill_switch()
            return "Kill switch active. New exposure blocked; no automatic liquidation."
        if action.action_type == PendingActionType.FLEET_EXIT:
            payload = action.payload
            if not isinstance(payload, dict):
                raise ValueError("Malformed fleet exit request.")
            positions = self.application.fleet_positions(str(payload["plan_id"]))
            if not positions:
                raise ValueError("Fleet positions changed; create a new preview.")
            result = await self.application.submit_fleet_exit(**payload)
            return f"Fleet exit accepted for {len(result)} managed positions."
        if action.action_type == PendingActionType.LAUNCH:
            payload = action.payload
            if not isinstance(payload, LaunchPendingPayload):
                raise ValueError("Malformed launch request.")
            plan = self.application.prepare_token_launch(payload.request)
            if plan.plan_id != payload.plan_id:
                raise ValueError("Launch plan changed; create a new preview.")
            result = await self.application.submit_token_launch(
                payload.request, **payload.submission_values
            )
            state = getattr(getattr(result, "state", None), "value", "unknown")
            return f"Launch submission state: {state}. Ambiguous remains ambiguous."
        raise ValueError("Unsupported pending action.")

    def _find_position(self, identity: str) -> Any:
        try:
            return self.application.get_position(identity)
        except (KeyError, StopIteration):
            matches = [
                item
                for item in self.application.list_positions()
                if str(item.accounting.token_mint) == identity
            ]
            if len(matches) != 1:
                raise KeyError(f"unknown or ambiguous position: {identity}") from None
            return matches[0]

    def _authorized(self, user_id: int, chat_type: str) -> bool:
        return user_id in self.config.owner_user_ids and (
            not self.config.private_chat_only or chat_type == "private"
        )

    async def _send(
        self,
        chat_id: int,
        text: str,
        buttons: Sequence[MessageButton] = (),
    ) -> None:
        for index, page in enumerate(split_message(text)):
            await self.transport.send_message(
                chat_id, page, buttons if index == 0 else ()
            )

    def _set_state(
        self, state: TelegramServiceState, reason: str | None = None
    ) -> None:
        self.state = state
        if self._state_observer is not None:
            mapped = {
                TelegramServiceState.STARTING: "starting",
                TelegramServiceState.CONNECTED: "connected",
                TelegramServiceState.DEGRADED: "degraded",
                TelegramServiceState.FAILED: "failed",
                TelegramServiceState.STOPPED: "stopped",
                TelegramServiceState.DISABLED: "disabled",
            }[state]
            self._state_observer(mapped, reason)


def _parse_command(text: str) -> tuple[str, tuple[str, ...]]:
    try:
        parts = shlex.split(text.strip())
    except ValueError as error:
        raise ValueError("Invalid command quoting.") from error
    if not parts or not parts[0].startswith("/"):
        raise ValueError("Commands must start with /. Use /help.")
    command = parts[0][1:].split("@", 1)[0].lower()
    return command, tuple(parts[1:])


def _parse_callback(data: str) -> tuple[str, str]:
    if not isinstance(data, str) or len(data) > 64 or data.count(":") != 1:
        raise ValueError("malformed callback")
    operation, nonce = data.split(":", 1)
    if operation not in {"confirm", "cancel"} or not nonce or len(nonce) > 32:
        raise ValueError("malformed callback")
    return operation, nonce


def _pending_error_text(code: str) -> str:
    return {
        "unknown": "Unknown or stale confirmation",
        "owner_mismatch": "Confirmation owner mismatch",
        "chat_mismatch": "Confirmation chat mismatch",
        "expired": "Confirmation expired",
        "replayed": "Confirmation already used",
        "pending_limit": "Too many pending confirmations",
    }.get(code, "Confirmation rejected")


def _safe_error(error: Exception) -> str:
    if isinstance(error, ExecutionError):
        if error.classification == ErrorClassification.RISK_LIMIT_EXCEEDED:
            normalized = error.message.lower()
            if "kill switch" in normalized:
                return "Kill switch is active; new exposure is blocked."
            if "trading" in normalized and "disabled" in normalized:
                return "Trading is disabled; new exposure is blocked."
            if "runtime" in normalized and "ready" in normalized:
                return "Hunter runtime is not ready for this action."
            return "Risk or runtime policy rejected the action."
        messages = {
            ErrorClassification.INSUFFICIENT_BALANCE: "Insufficient balance or wallet reserve.",
            ErrorClassification.FEE_RENT_INSUFFICIENCY: "Insufficient balance for fees or rent.",
            ErrorClassification.PROVIDER_UNAVAILABLE: "Execution provider is unavailable.",
            ErrorClassification.RPC_RATE_LIMIT: "Execution provider rate limit reached.",
            ErrorClassification.ACCEPTED_BUT_NOT_OBSERVED: "Execution was accepted but remains ambiguous.",
            ErrorClassification.CONFIRMATION_TIMEOUT: "Execution confirmation timed out and remains unresolved.",
            ErrorClassification.TRANSACTION_DROPPED: "Execution was dropped or is not yet observable.",
            ErrorClassification.UNSUPPORTED_QUOTE_TOKEN: "The requested quote token is unsupported.",
            ErrorClassification.CONFIGURATION_ERROR: "Required capability is not configured.",
        }
        return messages.get(
            error.classification,
            f"Execution rejected: {error.classification.value}.",
        )
    if isinstance(error, KeyError):
        return "Managed position or execution was not found."
    if isinstance(error, ValueError):
        return sanitize_text(error)[:500]
    return "Hunter could not complete the command. Check safe operational logs."
