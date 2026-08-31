"""Offline security, lifecycle, command, and notification tests for Telegram."""

# Test failures deliberately use direct operator-facing messages.
# ruff: noqa: TRY003

from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace

from solders.pubkey import Pubkey

from application.runtime import HunterApplication
from application.runtime_models import ApplicationState, RuntimeStatus
from domain.accounting import PositionAccounting
from domain.amounts import (
    BasisPoints,
    QuoteAmountRaw,
    RoundingDirection,
    TokenAmountRaw,
)
from domain.intents import TradeAction, TradeIntent, TradeIntentSource
from domain.lifecycle import ExecutionState
from execution.errors import ErrorClassification, ExecutionError
from storage.sqlite import StoredExecution, StoredPosition
from telegram_control.config import resolve_bot_token, telegram_config_from_dict
from telegram_control.factory import telegram_control_factories_from_config
from telegram_control.models import (
    IncomingCallback,
    IncomingMessage,
    NotificationPriority,
    PendingActionType,
    TelegramMetrics,
    TelegramNotification,
)
from telegram_control.notifications import TelegramNotificationQueue
from telegram_control.pending import PendingActionStore
from telegram_control.service import TelegramControlService
from telegram_control.views import safe_settings_text, split_message
from tests.test_runtime_composition import FakeTrader, base_config

OWNER = 123456789
MINT = Pubkey.from_string("11111111111111111111111111111112")
QUOTE = Pubkey.from_string("So11111111111111111111111111111111111111112")


class FakeEventBus:
    def __init__(self) -> None:
        self.consumers = []

    def subscribe(self, consumer) -> None:
        self.consumers.append(consumer)


class FakeTransport:
    def __init__(self) -> None:
        self.messages = []
        self.answers = []
        self.closed = False
        self.fail_sends = False

    async def start(self, message_handler, callback_handler, error_handler) -> None:
        self.message_handler = message_handler
        self.callback_handler = callback_handler
        self.error_handler = error_handler

    async def send_message(self, chat_id, text, buttons=()) -> None:
        if self.fail_sends:
            raise RuntimeError("offline Telegram failure")
        self.messages.append((chat_id, text, tuple(buttons)))

    async def answer_callback(self, callback_id, text="") -> None:
        self.answers.append((callback_id, text))

    async def close(self) -> None:
        self.closed = True


def runtime_status(**changes) -> RuntimeStatus:
    status = RuntimeStatus(
        application_state=ApplicationState.READY,
        application_ready=True,
        trading_enabled=True,
        kill_switch=False,
        entries_allowed=True,
        defensive_exits_allowed=True,
        recovery_complete=True,
        database_ready=True,
        blockhash_ready=True,
        fee_cache_ready=True,
        tip_cache_ready=False,
        listeners=(),
        providers=(),
        control_interfaces=(),
        tracked_wallet=None,
        active_bots=("hunter",),
        open_positions=1,
        active_fleets=0,
        pending_executions=0,
        pending_reconciliation=0,
        startup_timings=(),
        dropped_events=0,
    )
    return replace(status, **changes)


class FakeApplication:
    def __init__(self) -> None:
        self.bot_id = "hunter"
        self.config = {
            "platform": "pump_fun",
            "trade": {"exit_strategy": "tp_sl"},
            "filters": {"listener_type": "aggregate"},
        }
        self.event_bus = FakeEventBus()
        self.features = SimpleNamespace(token_launch_service=None)
        self.trader = SimpleNamespace()
        self.status = runtime_status()
        accounting = PositionAccounting("position-1", MINT, QUOTE, 6, 9)
        accounting.acquired_quantity_raw = 1_000_000
        accounting.quote_cost_raw = 1_000_000
        accounting.remaining_cost_basis_raw = 1_000_000
        self.position = StoredPosition(accounting, {}, 2_000, 1_000, 500)
        self.execution = StoredExecution(
            "execution-1",
            "position-1",
            "buy",
            ExecutionState.CONFIRMED,
            1,
            "signature-1",
            "blockhash",
            10,
            None,
            "created",
            "updated",
        )
        self.preview_calls = 0
        self.fail_revalidation = False
        self.submitted = []
        self.control_calls = []
        self.fleet_enabled = False

    def runtime_status(self):
        return self.status

    def enable_trading(self) -> None:
        self.control_calls.append("enable")
        self.status = replace(self.status, trading_enabled=True, entries_allowed=True)

    def disable_trading(self) -> None:
        self.control_calls.append("disable")
        self.status = replace(self.status, trading_enabled=False, entries_allowed=False)

    def activate_kill_switch(self) -> None:
        self.control_calls.append("kill")
        self.status = replace(
            self.status,
            trading_enabled=False,
            kill_switch=True,
            entries_allowed=False,
        )

    def list_positions(self):
        return [self.position]

    def get_position(self, position_id):
        if position_id != "position-1":
            raise KeyError(position_id)
        return self.position

    def get_realized_pnl(self, _position_id):
        return {
            "gross_raw": 0,
            "net_raw": 0,
            "remaining_cost_basis_raw": 1_000_000,
        }

    def list_executions(self, _limit=20):
        return [self.execution]

    def get_execution(self, identity):
        if identity not in {"execution-1", "signature-1"}:
            raise KeyError(identity)
        return self.execution

    def get_settings(self):
        return {"schema": 4, "endpoint_url": "https://secret.invalid/key"}

    def risk_settings(self):
        return {"enforce": True, "maximum_buy_raw": 1_000_000}

    async def wallet_balances(self):
        return ({"alias": "primary", "public_key": str(MINT), "lamports": 2_000_000},)

    def prepare_manual_buy(self, mint, amount, quote):
        del quote
        return TradeIntent(
            TradeAction.BUY,
            TradeIntentSource.MANUAL_BUY,
            Pubkey.from_string(mint),
            "primary",
            BasisPoints(100),
            quote_mint=QUOTE,
            token_decimals=6,
            quote_amount=QuoteAmountRaw.from_decimal(
                amount,
                mint=QUOTE,
                decimals=9,
                rounding=RoundingDirection.DOWN,
            ),
        )

    def prepare_manual_sell(self, position_id, percentage):
        if position_id != "position-1" or percentage not in {"50%", "100%"}:
            raise ValueError("invalid managed sell")
        quantity = 500_000 if percentage == "50%" else 1_000_000
        return TradeIntent(
            TradeAction.SELL,
            TradeIntentSource.MANUAL_SELL,
            MINT,
            "primary",
            BasisPoints(100),
            quote_mint=QUOTE,
            token_amount=TokenAmountRaw(quantity, MINT, 6),
            position_id=position_id,
        )

    def preview_manual_trade(self, intent):
        self.preview_calls += 1
        if self.fail_revalidation and self.preview_calls > 1:
            raise ExecutionError(ErrorClassification.RISK_LIMIT_EXCEEDED, "changed")
        return {"risk": "pass", "execution_mode": "managed"}

    async def submit_manual_trade(self, intent):
        self.submitted.append(intent)
        return SimpleNamespace(signature="offline-signature")

    def list_tracked_wallets(self):
        return ("tracked-public-address",)

    def fleet_positions(self, _plan_id):
        if not self.fleet_enabled:
            raise ValueError("wallet fleet runtime is disabled")
        return [
            {
                "fleet_position_id": "fleet-position-1",
                "status": "open",
                "remaining_quantity_raw": 10,
            }
        ]

    async def submit_fleet_exit(self, **values):
        self.submitted.append(values)
        return ["offline-fleet-result"]

    def prepare_token_launch(self, _request):
        if self.features.token_launch_service is None:
            raise ValueError("token launch runtime is disabled")
        return SimpleNamespace(
            plan_id="launch:offline",
            mint=MINT,
            execution_policy=SimpleNamespace(value="bundle"),
            components=(SimpleNamespace(quote_amount_raw=1_000),),
        )

    async def submit_token_launch(self, request, **values):
        if self.fail_revalidation:
            raise ExecutionError(ErrorClassification.RISK_LIMIT_EXCEEDED, "changed")
        self.submitted.append((request, values))
        return SimpleNamespace(state=SimpleNamespace(value="ambiguous"))


def telegram_config(**changes):
    values = {
        "enabled": True,
        "owner_user_ids": [OWNER],
        "confirmation_ttl_seconds": 45,
        "kill_confirmation_ttl_seconds": 20,
    }
    values.update(changes)
    return telegram_config_from_dict(values)


class TelegramSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.app = FakeApplication()
        self.transport = FakeTransport()
        self.service = TelegramControlService(
            telegram_config(), self.app, self.transport
        )
        await self.service.start()

    async def asyncTearDown(self) -> None:
        await self.service.close()

    async def command(self, text, *, user_id=OWNER, chat_type="private") -> None:
        await self.service.handle_message(
            IncomingMessage(user_id, user_id, chat_type, text, "same_username")
        )

    def callback_data(self, label="Confirm") -> str:
        buttons = self.transport.messages[-1][2]
        return next(item.callback_data for item in buttons if item.label == label)

    async def confirm(self, data=None, *, user_id=OWNER, chat_type="private") -> None:
        await self.service.handle_callback(
            IncomingCallback(
                "callback-id",
                user_id,
                OWNER,
                chat_type,
                data or self.callback_data(),
                "same_username",
            )
        )

    async def test_unauthorized_numeric_id_and_group_fail_before_dispatch(self) -> None:
        await self.command("/status", user_id=999)
        await self.command("/status", chat_type="supergroup")
        self.assertEqual(self.service.metrics.unauthorized_attempts, 2)
        self.assertNotIn(
            "Hunter status", "\n".join(item[1] for item in self.transport.messages)
        )

    async def test_read_only_command_surface_uses_application_models(self) -> None:
        for command in (
            "/start",
            "/help",
            "/status",
            "/balance",
            "/wallets",
            "/positions",
            "/position position-1",
            "/position 11111111111111111111111111111112",
            "/pnl",
            "/executions",
            "/tx signature-1",
            "/risk",
            "/providers",
            "/feeds",
            "/infra",
            "/settings",
            "/tracked",
            "/fleet",
        ):
            await self.command(command)
        text = "\n".join(item[1] for item in self.transport.messages)
        self.assertIn("Hunter status", text)
        self.assertIn("Network fees (lamports)", text)
        self.assertNotIn("secret.invalid", text)

    async def test_buy_requires_confirmation_and_revalidation(self) -> None:
        await self.command(f"/buy {MINT} 0.001")
        self.assertEqual(self.app.submitted, [])
        self.assertEqual(self.app.preview_calls, 1)
        await self.confirm()
        self.assertEqual(len(self.app.submitted), 1)
        self.assertEqual(self.app.preview_calls, 2)

    async def test_changed_risk_blocks_confirmed_buy(self) -> None:
        await self.command(f"/buy {MINT} 0.001")
        self.app.fail_revalidation = True
        await self.confirm()
        self.assertEqual(self.app.submitted, [])
        self.assertIn("Risk or runtime policy", self.transport.messages[-1][1])

    async def test_manual_sell_remains_application_authorized_during_halt(self) -> None:
        self.app.status = replace(
            self.app.status,
            trading_enabled=False,
            kill_switch=True,
            entries_allowed=False,
        )
        await self.command("/sell position-1 50%")
        await self.confirm()
        self.assertEqual(self.app.submitted[0].action, TradeAction.SELL)

    async def test_callback_is_owner_and_chat_bound(self) -> None:
        await self.command(f"/buy {MINT} 0.001")
        data = self.callback_data()
        await self.confirm(data, user_id=999)
        self.assertEqual(self.app.submitted, [])
        await self.service.handle_callback(
            IncomingCallback("wrong-chat", OWNER, 987654, "private", data)
        )
        self.assertEqual(self.app.submitted, [])

    async def test_malformed_callback_is_rejected(self) -> None:
        await self.service.handle_callback(
            IncomingCallback("malformed", OWNER, OWNER, "private", "buy:raw:parameters")
        )
        self.assertEqual(self.app.submitted, [])
        self.assertIn("Invalid action", self.transport.answers[-1][1])

    async def test_expired_buy_confirmation_is_rejected(self) -> None:
        self.service.config = replace(
            self.service.config, confirmation_ttl_seconds=0.001
        )
        await self.command(f"/buy {MINT} 0.001")
        data = self.callback_data()
        await asyncio.sleep(0.005)
        await self.confirm(data)
        self.assertEqual(self.app.submitted, [])
        self.assertEqual(self.service.metrics.pending_expired, 1)

    async def test_callback_is_single_use_under_concurrent_delivery(self) -> None:
        await self.command(f"/buy {MINT} 0.001")
        data = self.callback_data()
        await asyncio.gather(self.confirm(data), self.confirm(data))
        self.assertEqual(len(self.app.submitted), 1)
        self.assertEqual(self.service.metrics.callbacks_replayed, 1)

    async def test_cancel_prevents_execution(self) -> None:
        await self.command(f"/buy {MINT} 0.001")
        await self.confirm(self.callback_data("Cancel"))
        self.assertEqual(self.app.submitted, [])

    async def test_restart_invalidates_pending_confirmation(self) -> None:
        await self.command(f"/buy {MINT} 0.001")
        data = self.callback_data()
        replacement = TelegramControlService(
            telegram_config(), self.app, FakeTransport()
        )
        await replacement.start()
        await replacement.handle_callback(
            IncomingCallback("old", OWNER, OWNER, "private", data)
        )
        self.assertEqual(self.app.submitted, [])
        await replacement.close()

    async def test_runtime_controls_are_confirmed_except_explicit_kill(self) -> None:
        await self.command("/disable")
        await self.confirm()
        await self.command("/enable")
        await self.confirm()
        await self.command("/kill confirm")
        self.assertEqual(self.app.control_calls, ["disable", "enable", "kill"])

    async def test_kill_is_exempt_from_owner_rate_limit(self) -> None:
        self.service.rate_limiter = self.service.rate_limiter.__class__(1, 60)
        await self.command("/status")
        await self.command("/kill confirm")
        self.assertIn("kill", self.app.control_calls)

    async def test_launch_and_fleet_fail_closed_or_use_managed_service(self) -> None:
        await self.command("/launch anything")
        self.assertIn("not configured", self.transport.messages[-1][1])
        self.app.fleet_enabled = True
        await self.command("/fleet exit plan-1 manual parallel_fast 100")
        await self.confirm()
        self.assertEqual(self.app.submitted[-1]["plan_id"], "plan-1")

    async def test_configured_launch_is_staged_and_preserves_ambiguous_state(
        self,
    ) -> None:
        await self.service.close()
        self.app.features.token_launch_service = object()
        self.transport = FakeTransport()
        self.service = TelegramControlService(
            telegram_config(),
            self.app,
            self.transport,
            launch_resolver=lambda args: (args, {"estimated_cost": "offline"}),
        )
        await self.service.start()
        await self.command("/launch request-id")
        self.assertEqual(self.app.submitted, [])
        await self.confirm()
        self.assertIn("ambiguous", self.transport.messages[-1][1])

    async def test_launch_revalidates_runtime_policy_before_submission(self) -> None:
        await self.service.close()
        self.app.features.token_launch_service = object()
        self.transport = FakeTransport()
        self.service = TelegramControlService(
            telegram_config(),
            self.app,
            self.transport,
            launch_resolver=lambda args: (args, {}),
        )
        await self.service.start()
        await self.command("/launch request-id")
        self.app.fail_revalidation = True
        await self.confirm()
        self.assertEqual(self.app.submitted, [])

    async def test_missing_position_sell_fails_before_confirmation(self) -> None:
        await self.command("/sell missing 100%")
        self.assertEqual(self.service.pending.active_count, 0)
        self.assertEqual(self.app.submitted, [])

    async def test_shutdown_rejects_new_commands_and_cancels_pending(self) -> None:
        await self.command(f"/buy {MINT} 0.001")
        self.service.stop_accepting()
        await self.command("/status")
        self.assertEqual(self.service.pending.active_count, 0)
        self.assertIn("shutting down", self.transport.messages[-1][1])


class TelegramPrimitiveTests(unittest.IsolatedAsyncioTestCase):
    def test_disabled_default_and_fail_closed_owner_validation(self) -> None:
        self.assertFalse(telegram_config_from_dict(None).enabled)
        with self.assertRaisesRegex(ValueError, "owner"):
            telegram_config_from_dict({"enabled": True, "owner_user_ids": []})
        with self.assertRaisesRegex(ValueError, "positive integers"):
            telegram_config_from_dict({"enabled": True, "owner_user_ids": ["name"]})

    def test_token_is_environment_only_and_registered_for_redaction(self) -> None:
        config = telegram_config()
        token = resolve_bot_token(
            config, {"TELEGRAM_BOT_TOKEN": "123456:offline-secret"}
        )
        self.assertEqual(token, "123456:offline-secret")
        self.assertNotIn("offline-secret", safe_settings_text({"token": token}))
        with self.assertRaisesRegex(ValueError, "TELEGRAM_BOT_TOKEN"):
            resolve_bot_token(config, {})

    def test_disabled_factory_resolves_no_secret_or_transport(self) -> None:
        calls = []

        def record_transport(token):
            calls.append(token)

        factories = telegram_control_factories_from_config(
            {"enabled": False},
            environment={},
            transport_factory=record_transport,
        )
        self.assertEqual(factories, ())
        self.assertEqual(calls, [])

    def test_enabled_factory_requires_secret(self) -> None:
        with self.assertRaisesRegex(ValueError, "secret environment"):
            telegram_control_factories_from_config(
                {"enabled": True, "owner_user_ids": [OWNER]}, environment={}
            )

    def test_message_splitting_and_setting_redaction(self) -> None:
        pages = split_message("x" * 9000)
        self.assertTrue(all(len(item) <= 4096 for item in pages))
        rendered = safe_settings_text(
            {"rpc_endpoint": "https://example.invalid?api-key=secret", "mode": "safe"}
        )
        self.assertNotIn("api-key", rendered)
        self.assertIn("mode: safe", rendered)

    def test_terminal_confirmation_history_is_memory_bounded(self) -> None:
        store = PendingActionStore(maximum_pending=2)
        for index in range(20):
            action = store.create(
                owner_user_id=OWNER,
                chat_id=OWNER,
                action_type=PendingActionType.DISABLE,
                payload=None,
                summary=str(index),
                ttl_seconds=10,
            )
            store.cancel(action.nonce, owner_user_id=OWNER, chat_id=OWNER)
        self.assertLessEqual(len(store._actions), 8)

    async def test_priority_queue_preserves_critical_under_backpressure(self) -> None:
        transport = FakeTransport()
        metrics = TelegramMetrics()
        queue = TelegramNotificationQueue(
            transport, (OWNER,), metrics, maximum_pending=2
        )
        queue.publish_nowait(
            TelegramNotification("normal-1", NotificationPriority.NORMAL, "x")
        )
        queue.publish_nowait(
            TelegramNotification("normal-2", NotificationPriority.NORMAL, "x")
        )
        queue.publish_nowait(
            TelegramNotification("critical", NotificationPriority.CRITICAL, "x")
        )
        await queue.start()
        await asyncio.sleep(0)
        await queue.close(flush_timeout_seconds=1)
        text = [item[1] for item in transport.messages]
        self.assertIn("critical", text)
        self.assertEqual(metrics.notifications_dropped, 1)

    async def test_notification_failure_isolated_and_duplicate_dropped(self) -> None:
        transport = FakeTransport()
        transport.fail_sends = True
        metrics = TelegramMetrics()
        queue = TelegramNotificationQueue(
            transport, (OWNER,), metrics, maximum_pending=2
        )
        notification = TelegramNotification(
            "failure", NotificationPriority.HIGH, "x", "same-event"
        )
        self.assertTrue(queue.publish_nowait(notification))
        self.assertFalse(queue.publish_nowait(notification))
        await queue.start()
        await asyncio.sleep(0)
        await queue.close(flush_timeout_seconds=1)
        self.assertEqual(metrics.telegram_errors, 1)

    async def test_notification_close_waits_for_in_flight_send_with_bound(self) -> None:
        class SlowTransport(FakeTransport):
            def __init__(self) -> None:
                super().__init__()
                self.sending = asyncio.Event()
                self.release = asyncio.Event()

            async def send_message(self, chat_id, text, buttons=()) -> None:
                self.sending.set()
                await self.release.wait()
                await super().send_message(chat_id, text, buttons)

        transport = SlowTransport()
        queue = TelegramNotificationQueue(
            transport, (OWNER,), TelegramMetrics(), maximum_pending=2
        )
        queue.publish_nowait(
            TelegramNotification("critical", NotificationPriority.CRITICAL, "x")
        )
        await queue.start()
        await transport.sending.wait()
        closing = asyncio.create_task(queue.close(flush_timeout_seconds=1))
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        transport.release.set()
        await closing
        self.assertEqual(transport.messages[0][1], "critical")

    async def test_transport_failure_degrades_only_telegram(self) -> None:
        app = FakeApplication()
        service = TelegramControlService(telegram_config(), app, FakeTransport())
        states = []
        service.set_state_observer(lambda state, reason: states.append((state, reason)))
        await service.start()
        service.transport_failed(ConnectionError("offline"))
        self.assertEqual(states[-1], ("degraded", "ConnectionError"))
        self.assertTrue(app.status.application_ready)
        await service.close()

    async def test_optional_control_startup_does_not_delay_ready_runtime(self) -> None:
        class HangingControl:
            name = "telegram"

            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.stop_called = False
                self.observer = None

            def set_state_observer(self, observer) -> None:
                self.observer = observer

            async def start(self) -> None:
                self.observer("starting", None)
                self.started.set()
                await self.release.wait()

            def stop_accepting(self) -> None:
                self.stop_called = True

            async def close(self) -> None:
                self.release.set()

        control = HangingControl()
        app = HunterApplication(
            base_config(),
            FakeTrader(),
            control_interface_factories=(lambda _app: control,),
        )
        await app.start(start_detection=False)
        await asyncio.wait_for(control.started.wait(), timeout=1)
        self.assertTrue(app.runtime_status().application_ready)
        self.assertEqual(
            app.runtime_status().control_interfaces[0].state.value, "starting"
        )
        await app.shutdown(timeout_seconds=0.1)
        self.assertTrue(control.stop_called)

    async def test_application_manual_amounts_and_runtime_halt_policy(self) -> None:
        trader = FakeTrader()
        trader.buy_slippage = 0.05
        trader.sell_slippage = 0.07
        app = HunterApplication(base_config(), trader)
        await app.start(start_detection=False)
        buy = app.prepare_manual_buy(str(MINT), "0.001")
        self.assertEqual(buy.quote_amount.value, 1_000_000)
        self.assertEqual(buy.slippage.value, 500)

        accounting = PositionAccounting("position-1", MINT, QUOTE, 6, 9)
        accounting.acquired_quantity_raw = 1_000_000
        accounting.remaining_cost_basis_raw = 1_000_000
        trader.position_service.positions = [StoredPosition(accounting, {})]
        sell = app.prepare_manual_sell("position-1", "33.3333%")
        self.assertEqual(sell.token_amount.value, 333_300)
        self.assertEqual(sell.slippage.value, 700)

        app.activate_kill_switch()
        with self.assertRaises(ExecutionError):
            app.preview_manual_trade(buy)
        self.assertEqual(app.preview_manual_trade(sell)["source"], "manual_sell")
        await app.shutdown()


if __name__ == "__main__":
    unittest.main()
