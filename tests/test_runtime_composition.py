"""Offline characterization of Hunter's production composition root."""

# Test failures use deliberately direct exception construction.
# ruff: noqa: TRY003

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

from solders.pubkey import Pubkey

from application.risk import RiskLimits
from application.runtime import HunterApplication, HunterProcessRuntime
from application.runtime_events import ApplicationEventBus
from application.runtime_models import (
    ApplicationEvent,
    ApplicationState,
    RuntimeComponentState,
    TaskFailurePolicy,
)
from application.runtime_supervisor import RuntimeTaskSupervisor
from domain.amounts import BasisPoints, QuoteAmountRaw, TokenAmountRaw
from domain.intents import (
    EconomicActionClass,
    TradeAction,
    TradeIntent,
    TradeIntentSource,
)
from domain.wallet_tracking import WalletActivity, WalletActivityType
from execution.errors import ExecutionError
from interfaces.core import Platform, TokenInfo

MINT = Pubkey.from_string("11111111111111111111111111111112")
QUOTE = Pubkey.from_string("So11111111111111111111111111111111111111112")
TRACKED = Pubkey.from_string("11111111111111111111111111111113")


class FakeBlockhashCache:
    def current(self) -> object:
        return object()


class FakePositionService:
    def __init__(self) -> None:
        self.positions: list[object] = []
        self.open_mints: set[Pubkey] = set()

    def list_positions(self) -> list[object]:
        return list(self.positions)

    def get_position(self, position_id: str) -> object:
        return next(
            item
            for item in self.positions
            if item.accounting.position_id == position_id
        )

    def has_open_position(self, wallet_id: str, mint: Pubkey) -> bool:
        del wallet_id
        return mint in self.open_mints


class FakeStore:
    def __init__(self) -> None:
        self.claimed: set[str] = set()
        self.completed: dict[str, str] = {}

    def claim_wallet_event(self, event: WalletActivity, label: str | None) -> bool:
        del label
        if event.event_id in self.claimed:
            return False
        self.claimed.add(event.event_id)
        return True

    def complete_wallet_event(
        self,
        event_id: str,
        *,
        state: str,
        intent_id: str | None,
        reason: str | None,
    ) -> None:
        del intent_id, reason
        self.completed[event_id] = state


class FakeTrader:
    def __init__(self) -> None:
        self.platform = Platform.PUMP_FUN
        self.position_store = FakeStore()
        self.position_service = FakePositionService()
        self.risk_service = SimpleNamespace(limits=RiskLimits())
        self.readiness = None
        self.solana_client = SimpleNamespace(
            blockhash_cache=FakeBlockhashCache(), provider_warmup_results={}
        )
        self.priority_fee_manager = SimpleNamespace(last_selection=object())
        self.calls: list[str] = []
        self.intents: list[tuple[TradeIntent, TokenInfo | None]] = []
        self.detection_started = asyncio.Event()
        self.detection_release = asyncio.Event()
        self.detection_state_observer = None
        self.runtime_authorizer = None

    async def recover_runtime(self) -> None:
        self.calls.append("recover")

    async def warm_runtime(self) -> None:
        self.calls.append("warm")

    async def activate_runtime(self) -> None:
        self.calls.append("activate")

    async def run_detection(self) -> None:
        self.calls.append("detect")
        if self.detection_state_observer is not None:
            self.detection_state_observer("connected", None)
        self.detection_started.set()
        await self.detection_release.wait()

    async def shutdown_runtime(self) -> None:
        self.calls.append("shutdown")
        self.detection_release.set()

    async def execute_intent(
        self, intent: TradeIntent, token_info: TokenInfo | None = None
    ) -> object:
        if self.runtime_authorizer is not None:
            self.runtime_authorizer(
                intent.source,
                intent.action,
                managed_exit=(
                    intent.action == TradeAction.SELL and intent.position_id is not None
                ),
            )
        self.calls.append("execute")
        self.intents.append((intent, token_info))
        self.position_service.open_mints.add(intent.mint)
        return SimpleNamespace(success=True, signature="fake-signature")


class PausedBoundaryTrader(FakeTrader):
    """Pause between application dispatch and the final managed boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.boundary_reached = asyncio.Event()
        self.boundary_release = asyncio.Event()
        self.submissions = 0

    async def execute_intent(
        self, intent: TradeIntent, token_info: TokenInfo | None = None
    ) -> object:
        del token_info
        self.boundary_reached.set()
        await self.boundary_release.wait()
        if self.runtime_authorizer is not None:
            self.runtime_authorizer(
                intent.source,
                intent.action,
                managed_exit=(
                    intent.action == TradeAction.SELL and intent.position_id is not None
                ),
            )
        self.submissions += 1
        return SimpleNamespace(success=True, signature="fake-signature")


@dataclass
class FakeTrackedSource:
    service: object
    observer: object
    started: bool = False
    closed: bool = False

    async def start(self) -> None:
        self.started = True
        self.observer("connected", None)

    async def close(self) -> None:
        self.closed = True
        self.observer("stopped", None)

    async def emit(
        self, activity: WalletActivity, token_info: TokenInfo | None = None
    ) -> None:
        await self.service.handle(activity, token_info)


def base_config(*, tracking: bool = False) -> dict:
    config = {
        "name": "runtime-test",
        "rpc_endpoint": "https://rpc.invalid",
        "wss_endpoint": "wss://rpc.invalid",
        "filters": {"listener_type": "logs"},
        "runtime": {"event_queue_size": 8},
        "infrastructure": {"allow_degraded": False},
    }
    if tracking:
        config["wallet_tracking"] = {
            "enabled": True,
            "wallets": [
                {
                    "address": str(TRACKED),
                    "watch_create": True,
                    "watch_buy": True,
                    "create_action": {
                        "enabled": True,
                        "sizing_mode": "fixed",
                        "buy_amount_sol": "0.001",
                        "slippage_bps": 100,
                    },
                    "copy_action": {
                        "enabled": True,
                        "sizing_mode": "percentage_of_source",
                        "percentage_bps": 5000,
                    },
                }
            ],
        }
    return config


def buy_intent(source: TradeIntentSource = TradeIntentSource.YOLO) -> TradeIntent:
    return TradeIntent(
        action=TradeAction.BUY,
        source=source,
        mint=MINT,
        wallet_id="primary",
        quote_mint=QUOTE,
        quote_amount=QuoteAmountRaw(1_000_000, QUOTE, 9),
        token_decimals=6,
        slippage=BasisPoints(100),
        intent_id=f"intent:{source.value}",
    )


def sell_intent(
    source: TradeIntentSource = TradeIntentSource.MANUAL_SELL,
) -> TradeIntent:
    return TradeIntent(
        action=TradeAction.SELL,
        source=source,
        mint=MINT,
        wallet_id="primary",
        quote_mint=QUOTE,
        token_amount=TokenAmountRaw(1_000_000, MINT, 6),
        position_id="position:owned",
        slippage=BasisPoints(100),
        intent_id=f"intent:{source.value}",
    )


class RuntimeCompositionTests(unittest.IsolatedAsyncioTestCase):
    def test_all_intent_sources_have_explicit_exposure_classification(self) -> None:
        entries = {
            TradeIntentSource.LAUNCH_SNIPE,
            TradeIntentSource.TRACKED_WALLET_CREATE,
            TradeIntentSource.TRACKED_WALLET_BUY,
            TradeIntentSource.COPY_TRADE,
            TradeIntentSource.MANUAL_BUY,
            TradeIntentSource.YOLO,
            TradeIntentSource.TOKEN_LAUNCH,
            TradeIntentSource.LAUNCH_BUNDLE,
        }
        exits = set(TradeIntentSource) - entries
        self.assertTrue(
            all(
                source.economic_action_class == EconomicActionClass.ENTRY
                for source in entries
            )
        )
        self.assertTrue(
            all(
                source.economic_action_class == EconomicActionClass.EXIT
                for source in exits
            )
        )

    async def test_startup_orders_recovery_before_warm_and_activation(self) -> None:
        trader = FakeTrader()
        app = HunterApplication(base_config(), trader)
        await app.start(start_detection=False)
        self.assertEqual(trader.calls, ["recover", "warm", "activate"])
        self.assertTrue(app.recovery_complete)
        self.assertEqual(app.state, ApplicationState.READY)
        self.assertEqual(
            [item.phase for item in app.runtime_status().startup_timings],
            [
                "configuration_and_persistence",
                "recovery_running",
                "infrastructure_warming",
                "services_starting",
            ],
        )
        await app.shutdown()

    async def test_offline_composition_makes_no_socket_connection(self) -> None:
        app = HunterApplication(base_config(), FakeTrader())
        with patch("socket.socket.connect", side_effect=AssertionError("network")):
            await app.start(start_detection=False)
            await app.shutdown()

    async def test_recovery_barrier_blocks_intent(self) -> None:
        app = HunterApplication(base_config(), FakeTrader())
        with self.assertRaises(ExecutionError):
            await app.dispatch_intent(buy_intent())

    async def test_detection_is_supervised_after_ready(self) -> None:
        trader = FakeTrader()
        app = HunterApplication(base_config(), trader)
        await app.start()
        await trader.detection_started.wait()
        self.assertEqual(app.state, ApplicationState.READY)
        self.assertIn(
            ("detection:runtime-test", "critical", True), app.supervisor.snapshot()
        )
        await app.shutdown()
        self.assertEqual(app.state, ApplicationState.STOPPED)

    async def test_read_only_mode_runs_but_rejects_economic_intent(self) -> None:
        trader = FakeTrader()
        app = HunterApplication(base_config(), trader)
        await app.start(start_detection=False)
        app.disable_trading()
        self.assertEqual(app.state, ApplicationState.READY)
        with self.assertRaises(ExecutionError):
            await app.dispatch_intent(buy_intent())
        await app.shutdown()

    async def test_kill_switch_blocks_entries(self) -> None:
        app = HunterApplication(base_config(), FakeTrader())
        await app.start(start_detection=False)
        app.activate_kill_switch()
        with self.assertRaises(ExecutionError):
            await app.dispatch_intent(buy_intent())
        self.assertTrue(app.runtime_status().kill_switch)
        await app.shutdown()

    async def test_normal_runtime_allows_entries_and_managed_exits(self) -> None:
        trader = FakeTrader()
        app = HunterApplication(base_config(), trader)
        await app.start(start_detection=False)
        await app.dispatch_intent(buy_intent(TradeIntentSource.MANUAL_BUY))
        await app.dispatch_intent(sell_intent())
        self.assertEqual(len(trader.intents), 2)
        status = app.runtime_status()
        self.assertTrue(status.application_ready)
        self.assertTrue(status.entries_allowed)
        self.assertTrue(status.defensive_exits_allowed)
        await app.shutdown()

    async def test_trading_disabled_blocks_every_entry_class(self) -> None:
        app = HunterApplication(base_config(), FakeTrader())
        await app.start(start_detection=False)
        app.disable_trading()
        entries = {
            TradeIntentSource.LAUNCH_SNIPE,
            TradeIntentSource.TRACKED_WALLET_CREATE,
            TradeIntentSource.TRACKED_WALLET_BUY,
            TradeIntentSource.COPY_TRADE,
            TradeIntentSource.MANUAL_BUY,
            TradeIntentSource.YOLO,
        }
        for source in entries:
            with self.subTest(source=source), self.assertRaises(ExecutionError):
                app.authorize_economic_action(source, TradeAction.BUY)
        for source in {
            TradeIntentSource.TOKEN_LAUNCH,
            TradeIntentSource.LAUNCH_BUNDLE,
        }:
            with self.subTest(source=source), self.assertRaises(ExecutionError):
                app.authorize_economic_action(source, TradeAction.LAUNCH)
        status = app.runtime_status()
        self.assertFalse(status.entries_allowed)
        self.assertTrue(status.defensive_exits_allowed)
        await app.shutdown()

    async def test_trading_disabled_allows_every_managed_exit_class(self) -> None:
        app = HunterApplication(base_config(), FakeTrader())
        await app.start(start_detection=False)
        app.disable_trading()
        for source in {
            TradeIntentSource.MANUAL_SELL,
            TradeIntentSource.TAKE_PROFIT,
            TradeIntentSource.STOP_LOSS,
            TradeIntentSource.TIMED_EXIT,
            TradeIntentSource.EMERGENCY_EXIT,
            TradeIntentSource.WALLET_FLEET_EXIT,
        }:
            with self.subTest(source=source):
                app.authorize_economic_action(
                    source,
                    TradeAction.SELL,
                    managed_exit=True,
                )
        await app.shutdown()

    async def test_kill_switch_blocks_entries_but_allows_managed_exits(self) -> None:
        app = HunterApplication(base_config(), FakeTrader())
        await app.start(start_detection=False)
        app.activate_kill_switch()
        for source in {
            TradeIntentSource.LAUNCH_SNIPE,
            TradeIntentSource.TRACKED_WALLET_CREATE,
            TradeIntentSource.TRACKED_WALLET_BUY,
            TradeIntentSource.COPY_TRADE,
            TradeIntentSource.MANUAL_BUY,
            TradeIntentSource.YOLO,
        }:
            with self.subTest(source=source), self.assertRaises(ExecutionError):
                app.authorize_economic_action(source, TradeAction.BUY)
        for source in {
            TradeIntentSource.TOKEN_LAUNCH,
            TradeIntentSource.LAUNCH_BUNDLE,
        }:
            with self.subTest(source=source), self.assertRaises(ExecutionError):
                app.authorize_economic_action(source, TradeAction.LAUNCH)
        for source in {
            TradeIntentSource.MANUAL_SELL,
            TradeIntentSource.TAKE_PROFIT,
            TradeIntentSource.STOP_LOSS,
            TradeIntentSource.TIMED_EXIT,
            TradeIntentSource.EMERGENCY_EXIT,
            TradeIntentSource.WALLET_FLEET_EXIT,
        }:
            with self.subTest(source=source):
                app.authorize_economic_action(
                    source,
                    TradeAction.SELL,
                    managed_exit=True,
                )
        status = app.runtime_status()
        self.assertFalse(status.entries_allowed)
        self.assertTrue(status.defensive_exits_allowed)
        await app.shutdown()

    async def test_exit_authorization_requires_managed_position(self) -> None:
        app = HunterApplication(base_config(), FakeTrader())
        await app.start(start_detection=False)
        with self.assertRaisesRegex(ExecutionError, "managed position"):
            app.authorize_economic_action(
                TradeIntentSource.MANUAL_SELL,
                TradeAction.SELL,
            )
        with self.assertRaisesRegex(ValueError, "entry source"):
            app.authorize_economic_action(
                TradeIntentSource.MANUAL_BUY,
                TradeAction.SELL,
                managed_exit=True,
            )
        await app.shutdown()

    async def test_kill_switch_revalidates_entry_at_final_boundary(self) -> None:
        trader = PausedBoundaryTrader()
        app = HunterApplication(base_config(), trader)
        await app.start(start_detection=False)
        pending = asyncio.create_task(app.dispatch_intent(buy_intent()))
        await trader.boundary_reached.wait()
        app.activate_kill_switch()
        trader.boundary_release.set()
        with self.assertRaises(ExecutionError):
            await pending
        self.assertEqual(trader.submissions, 0)
        await app.shutdown()

    async def test_trading_disable_revalidates_entry_at_final_boundary(self) -> None:
        trader = PausedBoundaryTrader()
        app = HunterApplication(base_config(), trader)
        await app.start(start_detection=False)
        pending = asyncio.create_task(app.dispatch_intent(buy_intent()))
        await trader.boundary_reached.wait()
        app.disable_trading()
        trader.boundary_release.set()
        with self.assertRaises(ExecutionError):
            await pending
        self.assertEqual(trader.submissions, 0)
        await app.shutdown()

    async def test_stop_loss_remains_authorized_after_kill_switch(self) -> None:
        trader = PausedBoundaryTrader()
        app = HunterApplication(base_config(), trader)
        await app.start(start_detection=False)
        app.activate_kill_switch()
        pending = asyncio.create_task(
            app.dispatch_intent(sell_intent(TradeIntentSource.STOP_LOSS))
        )
        await trader.boundary_reached.wait()
        trader.boundary_release.set()
        result = await pending
        self.assertTrue(result.success)
        self.assertEqual(trader.submissions, 1)
        await app.shutdown()

    async def test_kill_switch_does_not_automatically_liquidate(self) -> None:
        trader = FakeTrader()
        app = HunterApplication(base_config(), trader)
        await app.start(start_detection=False)
        app.activate_kill_switch()
        await asyncio.sleep(0)
        self.assertEqual(trader.intents, [])
        await app.shutdown()

    async def test_successful_intent_uses_single_trader_boundary(self) -> None:
        trader = FakeTrader()
        app = HunterApplication(base_config(), trader)
        await app.start(start_detection=False)
        await app.dispatch_intent(buy_intent())
        self.assertEqual(len(trader.intents), 1)
        self.assertEqual(trader.intents[0][0].source, TradeIntentSource.YOLO)
        await app.shutdown()

    async def test_tracked_create_is_composed_and_immediate(self) -> None:
        trader = FakeTrader()
        holder: dict[str, FakeTrackedSource] = {}

        def factory(service: object, observer: object) -> FakeTrackedSource:
            source = FakeTrackedSource(service, observer)
            holder["source"] = source
            return source

        app = HunterApplication(
            base_config(tracking=True),
            trader,
            tracked_source_factory=factory,
        )
        await app.start(start_detection=False)
        token = TokenInfo("Token", "TOK", "", MINT, Platform.PUMP_FUN, quote_mint=QUOTE)
        activity = WalletActivity(
            WalletActivityType.CREATE,
            TRACKED,
            MINT,
            "create-signature",
            10,
            Pubkey.default(),
            quote_mint=QUOTE,
            token_decimals=6,
        )
        await holder["source"].emit(activity, token)
        self.assertEqual(len(trader.intents), 1)
        intent, context = trader.intents[0]
        self.assertEqual(intent.source, TradeIntentSource.TRACKED_WALLET_CREATE)
        self.assertIs(context, token)
        await app.shutdown()

    async def test_tracked_buy_uses_exact_percentage_and_durable_claim(self) -> None:
        trader = FakeTrader()
        holder: dict[str, FakeTrackedSource] = {}

        def factory(service: object, observer: object) -> FakeTrackedSource:
            source = FakeTrackedSource(service, observer)
            holder["source"] = source
            return source

        app = HunterApplication(
            base_config(tracking=True), trader, tracked_source_factory=factory
        )
        await app.start(start_detection=False)
        activity = WalletActivity(
            WalletActivityType.BUY,
            TRACKED,
            MINT,
            "buy-signature",
            11,
            Pubkey.default(),
            quote_mint=QUOTE,
            source_quote_amount=QuoteAmountRaw(1000, QUOTE, 9),
            token_decimals=6,
        )
        await holder["source"].emit(activity)
        await holder["source"].emit(activity)
        self.assertEqual(len(trader.intents), 1)
        self.assertEqual(trader.intents[0][0].quote_amount.value, 500)
        await app.shutdown()

    async def test_create_then_buy_default_policy_prevents_second_buy(self) -> None:
        trader = FakeTrader()
        holder: dict[str, FakeTrackedSource] = {}

        def factory(service: object, observer: object) -> FakeTrackedSource:
            source = FakeTrackedSource(service, observer)
            holder["source"] = source
            return source

        app = HunterApplication(
            base_config(tracking=True), trader, tracked_source_factory=factory
        )
        await app.start(start_detection=False)
        create = WalletActivity(
            WalletActivityType.CREATE,
            TRACKED,
            MINT,
            "create",
            1,
            Pubkey.default(),
            quote_mint=QUOTE,
            token_decimals=6,
        )
        buy = WalletActivity(
            WalletActivityType.BUY,
            TRACKED,
            MINT,
            "buy",
            2,
            Pubkey.default(),
            quote_mint=QUOTE,
            source_quote_amount=QuoteAmountRaw(1000, QUOTE, 9),
            token_decimals=6,
        )
        await holder["source"].emit(create)
        await holder["source"].emit(buy)
        self.assertEqual(len(trader.intents), 1)
        await app.shutdown()

    async def test_feature_gate_does_not_construct_tracker(self) -> None:
        app = HunterApplication(base_config(tracking=False), FakeTrader())
        self.assertIsNone(app.features.tracked_wallet_source)
        self.assertEqual(app.list_tracked_wallets(), ())
        await app.shutdown()

    async def test_tracked_runtime_status_has_connection_state(self) -> None:
        def factory(service: object, observer: object) -> FakeTrackedSource:
            return FakeTrackedSource(service, observer)

        app = HunterApplication(
            base_config(tracking=True),
            FakeTrader(),
            tracked_source_factory=factory,
        )
        await app.start(start_detection=False)
        self.assertEqual(
            app.runtime_status().tracked_wallet.state,
            RuntimeComponentState.CONNECTED,
        )
        await app.shutdown()

    async def test_multi_bot_runtime_keeps_state_isolated(self) -> None:
        first = HunterApplication({**base_config(), "name": "first"}, FakeTrader())
        second = HunterApplication({**base_config(), "name": "second"}, FakeTrader())
        runtime = HunterProcessRuntime((first, second))
        await asyncio.gather(
            first.start(start_detection=False), second.start(start_detection=False)
        )
        first.disable_trading()
        self.assertFalse(first.trading_enabled)
        self.assertTrue(second.trading_enabled)
        self.assertEqual(len(runtime.runtime_status()), 2)
        await runtime.shutdown()

    async def test_duplicate_bot_names_fail_before_start(self) -> None:
        first = HunterApplication(base_config(), FakeTrader())
        second = HunterApplication(base_config(), FakeTrader())
        with self.assertRaises(ValueError):
            HunterProcessRuntime((first, second))
        await first.shutdown()
        await second.shutdown()

    async def test_required_sender_warmup_failure_is_not_ready(self) -> None:
        config = base_config()
        config["execution"] = {
            "enabled": True,
            "providers": [
                {
                    "id": "required-rpc",
                    "kind": "standard_rpc",
                    "endpoint": "https://rpc.invalid",
                    "roles": ["submit"],
                    "required": True,
                }
            ],
        }
        trader = FakeTrader()
        trader.solana_client.provider_warmup_results = {"required-rpc": False}
        app = HunterApplication(config, trader)
        await app.start(start_detection=False)
        self.assertEqual(app.state, ApplicationState.NOT_READY)
        with self.assertRaises(ExecutionError):
            await app.dispatch_intent(buy_intent())
        await app.shutdown()

    async def test_explicit_degraded_mode_allows_valid_remaining_route(self) -> None:
        config = base_config()
        config["infrastructure"]["allow_degraded"] = True
        config["execution"] = {
            "enabled": True,
            "providers": [
                {
                    "id": "required-rpc",
                    "kind": "standard_rpc",
                    "endpoint": "https://rpc.invalid",
                    "roles": ["submit"],
                    "required": True,
                }
            ],
        }
        trader = FakeTrader()
        trader.solana_client.provider_warmup_results = {"required-rpc": False}
        app = HunterApplication(config, trader)
        await app.start(start_detection=False)
        self.assertEqual(app.state, ApplicationState.DEGRADED)
        await app.dispatch_intent(buy_intent())
        await app.shutdown()

    async def test_enabled_fleet_without_signer_composition_fails_early(self) -> None:
        config = base_config()
        config["wallet_fleet"] = {
            "enabled": True,
            "wallets": [{"id": "creator", "signer": "env:CREATOR_KEY"}],
            "launch": {"risk_enforced": True},
        }
        with self.assertRaisesRegex(ValueError, "signer and orchestration"):
            HunterApplication(config, FakeTrader())

    async def test_bundle_launch_without_bundle_provider_fails_early(self) -> None:
        config = base_config()
        config["wallet_fleet"] = {
            "enabled": True,
            "wallets": [{"id": "creator", "signer": "env:CREATOR_KEY"}],
            "launch": {"risk_enforced": True},
        }
        config["token_launch"] = {
            "enabled": True,
            "execution": {"mode": "bundle"},
            "exit": {"type": "manual"},
        }
        with self.assertRaisesRegex(ValueError, "bundle-capable"):
            HunterApplication(config, FakeTrader())

    async def test_launch_and_fleet_services_compose_with_explicit_factory(
        self,
    ) -> None:
        config = base_config()
        config["execution"] = {
            "enabled": True,
            "providers": [
                {
                    "id": "jito-bundle",
                    "kind": "jito",
                    "endpoint": "https://jito.invalid",
                    "roles": ["submit"],
                }
            ],
        }
        config["wallet_fleet"] = {
            "enabled": True,
            "wallets": [{"id": "creator", "signer": "env:CREATOR_KEY"}],
            "launch": {"risk_enforced": True},
        }
        config["token_launch"] = {
            "enabled": True,
            "execution": {"mode": "bundle"},
            "exit": {"type": "manual"},
        }
        services = (object(), object(), object())

        def compose_orchestration(*_args: object) -> tuple[object, object, object]:
            return services

        app = HunterApplication(
            config,
            FakeTrader(),
            orchestration_factory=compose_orchestration,
        )
        self.assertIs(app.features.token_launch_service, services[0])
        self.assertIs(app.features.wallet_fleet_service, services[1])
        self.assertIs(app.features.wallet_fleet_exit_service, services[2])
        await app.shutdown()

    async def test_critical_detection_crash_revokes_readiness(self) -> None:
        trader = FakeTrader()

        async def crash() -> None:
            if trader.detection_state_observer is not None:
                trader.detection_state_observer("connected", None)
            raise RuntimeError("offline detector crash")

        trader.run_detection = crash
        app = HunterApplication(base_config(), trader)
        await app.start()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(app.state, ApplicationState.NOT_READY)
        await app.shutdown()


class RuntimePrimitiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_bus_is_bounded_and_observes_drops(self) -> None:
        bus = ApplicationEventBus(maximum_pending=1)
        first = bus.publish_nowait(ApplicationEvent("one", "bot"))
        second = bus.publish_nowait(ApplicationEvent("two", "bot"))
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(bus.dropped_events, 1)
        await bus.start()
        await bus.close()

    async def test_event_consumer_failure_does_not_stop_dispatch(self) -> None:
        bus = ApplicationEventBus()

        async def broken(event: ApplicationEvent) -> None:
            del event
            raise RuntimeError("offline failure")

        bus.subscribe(broken)
        await bus.start()
        bus.publish_nowait(ApplicationEvent("event", "bot"))
        await bus.close()
        self.assertEqual(bus.consumer_failures, 1)

    async def test_critical_task_failure_is_observable(self) -> None:
        failures: list[tuple[str, TaskFailurePolicy, str]] = []

        def observe(name: str, policy: TaskFailurePolicy, error: BaseException) -> None:
            failures.append((name, policy, type(error).__name__))

        supervisor = RuntimeTaskSupervisor(observe)

        async def fail() -> None:
            raise RuntimeError("offline")

        task = supervisor.create("critical", fail(), policy=TaskFailurePolicy.CRITICAL)
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertEqual(
            failures, [("critical", TaskFailurePolicy.CRITICAL, "RuntimeError")]
        )
        await supervisor.shutdown()

    async def test_shutdown_cancels_owned_tasks(self) -> None:
        supervisor = RuntimeTaskSupervisor(lambda *_: None)
        stopped = asyncio.Event()

        async def wait_forever() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        supervisor.create("worker", wait_forever(), policy=TaskFailurePolicy.OPTIONAL)
        await asyncio.sleep(0)
        await supervisor.shutdown()
        self.assertTrue(stopped.is_set())


if __name__ == "__main__":
    unittest.main()
