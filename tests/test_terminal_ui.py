# Test doubles deliberately exercise direct error paths and lazy imports.
# ruff: noqa: FBT003, PLC0415, TRY003

import asyncio
import inspect
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from application.runtime_models import (
    ApplicationEvent,
    ApplicationState,
    ComponentStatus,
    RuntimeComponentState,
    RuntimeStatus,
)
from terminal_ui.config import TerminalUIConfig, terminal_ui_config_from_dict
from terminal_ui.events import TerminalEventBuffer
from terminal_ui.factory import terminal_ui_factories_from_config
from terminal_ui.models import OperatorAction, TerminalView, ValueState
from terminal_ui.service import (
    TerminalConfirmationError,
    TerminalControlService,
    UnsupportedTerminalControlError,
    performance_snapshot,
)
from terminal_ui.views import render_view


def runtime_status(**changes):
    values = {
        "application_state": ApplicationState.READY,
        "application_ready": True,
        "trading_enabled": True,
        "kill_switch": False,
        "entries_allowed": True,
        "defensive_exits_allowed": True,
        "recovery_complete": True,
        "database_ready": True,
        "blockhash_ready": True,
        "fee_cache_ready": True,
        "tip_cache_ready": None,
        "listeners": (
            ComponentStatus("logs", RuntimeComponentState.RECEIVING, required=True),
        ),
        "providers": (
            ComponentStatus("rpc-a", RuntimeComponentState.WARM, required=True),
        ),
        "control_interfaces": (),
        "tracked_wallet": None,
        "active_bots": ("hunter-test",),
        "open_positions": 0,
        "active_fleets": 0,
        "pending_executions": 0,
        "pending_reconciliation": 0,
        "startup_timings": (),
        "dropped_events": 0,
    }
    values.update(changes)
    return RuntimeStatus(**values)


class FakeEventBus:
    def __init__(self):
        self.consumers = []

    def subscribe(self, consumer):
        self.consumers.append(consumer)


class FakeApplication:
    def __init__(self):
        self.bot_id = "hunter-test"
        self.config = {"execution": {"mode": "hedged"}}
        self.event_bus = FakeEventBus()
        self.features = SimpleNamespace()
        self.status = runtime_status()
        self.positions = list(range(60))
        self.executions = [
            SimpleNamespace(logical_execution_id=f"e-{i}") for i in range(80)
        ]
        self.telemetry = []
        self.shutdown_calls = 0
        self.position_page_calls = []

    def runtime_status(self):
        return self.status

    def enable_trading(self):
        if self.status.kill_switch:
            raise ValueError("cannot enable while kill switch is active")
        self.status = runtime_status(trading_enabled=True, entries_allowed=True)

    def disable_trading(self):
        self.status = runtime_status(trading_enabled=False, entries_allowed=False)

    def activate_kill_switch(self):
        self.status = runtime_status(
            trading_enabled=False, kill_switch=True, entries_allowed=False
        )

    def list_positions_page(self, *, limit=25, offset=0):
        self.position_page_calls.append((limit, offset))
        return self.positions[offset : offset + limit]

    def list_executions(self, limit=20):
        return self.executions[:limit]

    def list_recent_telemetry(self, limit=200):
        return self.telemetry[:limit]

    def risk_settings(self):
        return {"enforce": True, "maximum_priority_fee_lamports": 5000}

    def get_settings(self):
        return {"theme": "dark", "rpc_url": "https://secret.invalid/key"}

    def operator_settings(self):
        return {"buy_slippage": 0.1, "execution_mode": "hedged"}

    def list_tracked_wallets(self):
        return ("wallet-a",)

    async def wallet_balances(self):
        return ({"alias": "primary", "public_key": "public", "lamports": 5},)

    async def shutdown(self):
        self.shutdown_calls += 1


class FakeDriver:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.snapshots = []
        self.callbacks = {}

    async def start(self, **callbacks):
        self.callbacks = callbacks
        self.started.set()
        if self.fail:
            raise RuntimeError("render failed")
        await self.closed.wait()

    async def wait_ready(self):
        await self.started.wait()

    def publish_snapshot(self, snapshot):
        self.snapshots.append(snapshot)

    async def close(self):
        self.closed.set()


class NeverReadyDriver(FakeDriver):
    async def wait_ready(self):
        await asyncio.Event().wait()


class TerminalConfigTests(unittest.TestCase):
    def test_disabled_factory_does_not_import_textual_adapter(self):
        sys.modules.pop("terminal_ui.textual_adapter", None)
        self.assertEqual(terminal_ui_factories_from_config(None), ())
        self.assertNotIn("terminal_ui.textual_adapter", sys.modules)

    def test_configuration_is_disabled_by_default_and_bounded(self):
        self.assertFalse(terminal_ui_config_from_dict(None).enabled)
        with self.assertRaises(ValueError):
            terminal_ui_config_from_dict({"enabled": True, "event_queue_size": 0})


class TerminalServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.application = FakeApplication()
        self.driver = FakeDriver()
        self.service = TerminalControlService(
            terminal_ui_config_from_dict(
                {
                    "enabled": True,
                    "refresh_interval_seconds": 60,
                    "balance_refresh_seconds": 60,
                    "event_queue_size": 8,
                    "event_history_size": 10,
                    "position_page_size": 10,
                    "execution_page_size": 10,
                }
            ),
            self.application,
            self.driver,
        )
        self.states = []
        self.service.set_state_observer(
            lambda state, reason: self.states.append((state, reason))
        )

    async def asyncTearDown(self):
        await self.service.close()

    async def test_start_is_non_blocking_and_subscribes_after_explicit_start(self):
        self.assertEqual(self.application.event_bus.consumers, [])
        await self.service.start()
        await asyncio.wait_for(self.driver.started.wait(), 1)
        self.assertEqual(len(self.application.event_bus.consumers), 1)
        self.assertIn(("ready", None), self.states)

    async def test_snapshot_uses_bounded_queries_and_sanitizes_settings(self):
        snapshot = await self.service.refresh_once()
        self.assertEqual(self.application.position_page_calls, [(10, 0)])
        self.assertEqual(len(snapshot.positions), 10)
        self.assertEqual(len(snapshot.executions), 10)
        self.assertEqual(snapshot.routing_mode, "hedged")
        self.assertNotIn("rpc_url", snapshot.settings)
        self.assertEqual(snapshot.settings["buy_slippage"], 0.1)
        self.assertEqual(snapshot.wallets[0]["lamports"], 5)

    async def test_old_telemetry_is_marked_stale_not_zero(self):
        self.application.telemetry = [{"processed_at": "2020-01-01T00:00:00+00:00"}]
        snapshot = await self.service.refresh_once()
        self.assertEqual(snapshot.telemetry_freshness, ValueState.STALE)
        self.assertEqual(snapshot.performance.build_ms.state, ValueState.UNMEASURED)

    async def test_refresh_requests_are_coalesced(self):
        self.service.request_refresh()
        self.service.request_refresh()
        snapshot = await self.service.refresh_once()
        self.assertGreaterEqual(snapshot.refresh_coalesced, 1)

    async def test_enable_disable_and_kill_require_single_use_confirmation(self):
        await self.service.start()
        disable = await self.service.stage_action(OperatorAction.DISABLE_TRADING)
        self.assertTrue(self.application.status.trading_enabled)
        await self.service.confirm_action(disable.nonce)
        self.assertFalse(self.application.status.trading_enabled)
        with self.assertRaises(TerminalConfirmationError):
            await self.service.confirm_action(disable.nonce)

        enable = await self.service.stage_action(OperatorAction.ENABLE_TRADING)
        await self.service.confirm_action(enable.nonce)
        self.assertTrue(self.application.status.trading_enabled)

        kill = await self.service.stage_action(OperatorAction.ACTIVATE_KILL_SWITCH)
        await self.service.confirm_action(kill.nonce)
        self.assertTrue(self.application.status.kill_switch)
        self.assertFalse(self.application.status.entries_allowed)
        self.assertTrue(self.application.status.defensive_exits_allowed)

    async def test_control_revalidates_state_at_confirmation_boundary(self):
        await self.service.start()
        enable = await self.service.stage_action(OperatorAction.ENABLE_TRADING)
        self.application.activate_kill_switch()
        with self.assertRaises(TerminalConfirmationError):
            await self.service.confirm_action(enable.nonce)

    async def test_clear_kill_switch_is_confirmed_then_fails_closed(self):
        await self.service.start()
        self.application.activate_kill_switch()
        clear = await self.service.stage_action(OperatorAction.CLEAR_KILL_SWITCH)
        with self.assertRaises(UnsupportedTerminalControlError):
            await self.service.confirm_action(clear.nonce)
        self.assertTrue(self.application.status.kill_switch)

    async def test_expired_confirmation_cannot_apply_control(self):
        await self.service.start()
        with patch("terminal_ui.service.monotonic", return_value=10.0):
            confirmation = await self.service.stage_action(
                OperatorAction.DISABLE_TRADING
            )
        with (
            patch("terminal_ui.service.monotonic", return_value=31.0),
            self.assertRaises(TerminalConfirmationError),
        ):
            await self.service.confirm_action(confirmation.nonce)
        self.assertTrue(self.application.status.trading_enabled)

    async def test_ui_exit_does_not_shutdown_hunter(self):
        await self.service.start()
        self.driver.callbacks["request_exit"]()
        await self.service.close()
        self.assertEqual(self.application.shutdown_calls, 0)

    async def test_ui_failure_is_reported_without_touching_runtime_readiness(self):
        driver = FakeDriver(fail=True)
        service = TerminalControlService(self.service.config, self.application, driver)
        states = []
        service.set_state_observer(lambda state, reason: states.append((state, reason)))
        with self.assertRaises(RuntimeError):
            await service.start()
        self.assertTrue(self.application.status.application_ready)
        await service.close()

    async def test_startup_timeout_is_isolated_from_runtime_readiness(self):
        config = TerminalUIConfig(enabled=True, startup_timeout_seconds=0.01)
        driver = NeverReadyDriver()
        service = TerminalControlService(config, self.application, driver)
        with self.assertRaises(TimeoutError):
            await service.start()
        self.assertTrue(self.application.status.application_ready)
        await service.close()

    async def test_event_consumer_is_bounded_and_reports_drops(self):
        for index in range(12):
            await self.service._consume_event(
                ApplicationEvent("event", "hunter-test", {"index": index})
            )
        snapshot = await self.service.refresh_once()
        self.assertEqual(len(snapshot.recent_events), 8)
        self.assertEqual(snapshot.event_drops, 4)

    async def test_pagination_uses_bounded_offsets(self):
        self.service.change_page("positions", 1)
        snapshot = await self.service.refresh_once()
        self.assertEqual(snapshot.positions, tuple(range(10, 20)))
        self.assertEqual(self.application.position_page_calls[-1], (10, 10))
        self.service.change_page("positions", -1)
        self.assertEqual(self.service._position_offset, 0)

    async def test_textual_dashboard_mounts_offline_and_closes_without_hunter(self):
        from terminal_ui.textual_adapter import HunterTerminalApp

        snapshot = await self.service.refresh_once()

        async def stage(action):
            return await self.service.stage_action(action)

        app = HunterTerminalApp(
            request_action=stage,
            confirm_action=self.service.confirm_action,
            request_refresh=self.service.request_refresh,
            request_exit=self.service.stop_accepting,
            request_page=self.service.change_page,
        )
        app.apply_snapshot(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertIn(
                "HUNTER OPERATOR OVERVIEW", str(app.query_one("#content").render())
            )
        self.assertEqual(self.application.shutdown_calls, 0)


class TerminalPresentationTests(unittest.TestCase):
    def test_missing_metrics_are_unmeasured_not_zero(self):
        result = performance_snapshot([])
        self.assertEqual(result.build_ms.state, ValueState.UNMEASURED)
        self.assertIsNone(result.same_slot)

    def test_metrics_require_sufficient_samples_for_percentiles(self):
        rows = [
            {
                "build_started_mono_ns": index * 10,
                "build_completed_mono_ns": index * 10 + 1_000_000,
                "submitted_slot": 100,
                "landed_slot": 100 + (index % 3),
            }
            for index in range(20)
        ]
        result = performance_snapshot(rows)
        self.assertEqual(result.percentiles["build"]["p95"].state, ValueState.AVAILABLE)
        self.assertEqual(
            result.percentiles["build"]["p99"].state, ValueState.INSUFFICIENT
        )
        self.assertEqual(
            (result.same_slot, result.next_slot, result.later_slot), (7, 7, 6)
        )

    def test_degraded_provider_and_missing_values_render_explicitly(self):
        app = FakeApplication()
        app.status = runtime_status(
            providers=(
                ComponentStatus(
                    "rpc-a", RuntimeComponentState.DEGRADED, True, "timeout"
                ),
            ),
            blockhash_ready=None,
        )
        snapshot = SimpleNamespace(
            status=app.status,
            routing_mode="single",
        )
        text = render_view(snapshot, TerminalView.PROVIDERS)
        self.assertIn("degraded", text)
        self.assertIn("unavailable", text)

    def test_no_provider_or_submission_import_in_terminal_modules(self):
        import terminal_ui.service as service_module
        import terminal_ui.textual_adapter as adapter_module

        source = inspect.getsource(service_module) + inspect.getsource(adapter_module)
        self.assertNotIn("execution.providers", source)
        self.assertNotIn("build_and_send_transaction", source)
        self.assertNotIn("submit_manual_trade", source)


class TerminalEventBufferTests(unittest.TestCase):
    def test_buffer_drops_oldest_without_blocking(self):
        buffer = TerminalEventBuffer(2, 10)
        for index in range(3):
            buffer.publish_nowait(ApplicationEvent(str(index), "hunter"))
        self.assertEqual(buffer.dropped, 1)
        self.assertEqual([item.event_type for item in buffer.drain()], ["1", "2"])
