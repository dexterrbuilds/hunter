"""Hunter application composition root and production lifecycle boundary."""

# Composition performs explicit configuration mapping by design.
# ruff: noqa: C901, TRY003

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from time import monotonic_ns
from typing import Any, Protocol
from uuid import uuid4

from solders.pubkey import Pubkey

from application.milestone37_config import (
    validate_token_launch_config,
    validate_wallet_fleet_config,
    wallet_tracking_config_from_dict,
)
from application.runtime_events import ApplicationEventBus
from application.runtime_models import (
    ApplicationEvent,
    ApplicationState,
    ComponentStatus,
    RuntimeComponentState,
    RuntimeStatus,
    StartupPhaseTiming,
    TaskFailurePolicy,
)
from application.runtime_supervisor import RuntimeTaskSupervisor
from application.wallet_tracking import AsyncWalletEventStore, TrackedWalletService
from core.pubkeys import TOKEN_DECIMALS, WSOL_MINT, quote_decimals, resolve_quote_mint
from domain.amounts import (
    BasisPoints,
    QuoteAmountRaw,
    RoundingDirection,
    TokenAmountRaw,
)
from domain.intents import (
    EconomicActionClass,
    ExecutionUrgency,
    TradeAction,
    TradeIntent,
    TradeIntentSource,
    classify_economic_action,
    default_urgency,
)
from domain.launch import LaunchExecutionPlan, TokenLaunchRequest
from execution.errors import ErrorClassification, ExecutionError
from execution.providers.config import ProviderRole
from execution.providers.factory import routing_config_from_dict
from interfaces.core import Platform, TokenInfo
from monitoring.tracked_wallets import (
    PumpFunWalletActivityDecoder,
    TrackedWalletLogSource,
    TrackedWalletProcessor,
)
from utils.idl_manager import IDLManager


class RuntimeTrader(Protocol):
    """Lifecycle surface implemented by the existing UniversalTrader."""

    platform: Platform
    position_service: Any
    position_store: Any
    risk_service: Any
    readiness: Any
    solana_client: Any
    priority_fee_manager: Any

    async def recover_runtime(self) -> None: ...

    async def warm_runtime(self) -> None: ...

    async def activate_runtime(self) -> None: ...

    async def run_detection(self) -> None: ...

    async def shutdown_runtime(self) -> None: ...

    async def execute_intent(
        self, intent: TradeIntent, token_info: TokenInfo | None = None
    ) -> object: ...


class RuntimeControlInterface(Protocol):
    """Optional interface lifecycle; trading never depends on its readiness."""

    name: str

    def set_state_observer(
        self, observer: Callable[[str, str | None], None]
    ) -> None: ...

    async def start(self) -> None: ...

    def stop_accepting(self) -> None: ...

    async def close(self) -> None: ...


TraderFactory = Callable[[dict[str, Any]], RuntimeTrader]
OrchestrationFactory = Callable[
    [
        dict[str, Any],
        RuntimeTrader,
        Callable[[TradeIntent, TokenInfo | None], Awaitable[object]],
    ],
    tuple[object | None, object | None, object | None],
]
ControlInterfaceFactory = Callable[["HunterApplication"], RuntimeControlInterface]
_BASIS_POINTS_SCALE = 10_000


@dataclass(slots=True)
class RuntimeFeatures:
    """Feature-gated services owned by one bot runtime."""

    tracked_wallet_service: TrackedWalletService | None = None
    tracked_wallet_source: TrackedWalletLogSource | None = None
    token_launch_service: object | None = None
    wallet_fleet_service: object | None = None
    wallet_fleet_exit_service: object | None = None


class HunterApplication:
    """Compose, recover, warm, supervise, and stop one Hunter bot instance."""

    def __init__(  # noqa: PLR0913
        self,
        config: dict[str, Any],
        trader: RuntimeTrader,
        *,
        event_bus: ApplicationEventBus | None = None,
        tracked_source_factory: Callable[
            [TrackedWalletService, Callable[[str, str | None], None]],
            TrackedWalletLogSource,
        ]
        | None = None,
        orchestration_factory: OrchestrationFactory | None = None,
        control_interface_factories: tuple[ControlInterfaceFactory, ...] = (),
    ) -> None:
        self.config = config
        self.bot_id = str(config.get("name", "hunter"))
        self.trader = trader
        self.state = ApplicationState.CREATED
        self.recovery_complete = False
        limits = trader.risk_service.limits
        self.trading_enabled = bool(limits.trading_enabled)
        self.kill_switch = bool(limits.emergency_kill_switch)
        self.allow_degraded = bool(
            config.get("infrastructure", {}).get("allow_degraded", False)
        )
        self.event_bus = event_bus or ApplicationEventBus(
            int(config.get("runtime", {}).get("event_queue_size", 512))
        )
        self.supervisor = RuntimeTaskSupervisor(self._task_failed)
        self.features = RuntimeFeatures()
        self._startup_timings: list[StartupPhaseTiming] = []
        self._listener_states: dict[str, ComponentStatus] = {}
        self._provider_states: dict[str, ComponentStatus] = {}
        self._control_states: dict[str, ComponentStatus] = {}
        self._control_interfaces: list[RuntimeControlInterface] = []
        self._shutdown_lock = asyncio.Lock()
        self._started = False
        self._tracked_source_factory = tracked_source_factory
        self._orchestration_factory = orchestration_factory
        if hasattr(self.trader, "detection_state_observer"):
            self.trader.detection_state_observer = self._detection_state_changed
        if hasattr(self.trader, "runtime_authorizer"):
            self.trader.runtime_authorizer = self.authorize_economic_action
        composition_started = monotonic_ns()
        self._validate_and_compose()
        self._compose_control_interfaces(control_interface_factories)
        self._startup_timings.append(
            StartupPhaseTiming(
                "configuration_and_persistence",
                (monotonic_ns() - composition_started) / 1_000_000,
            )
        )

    @classmethod
    def compose(
        cls,
        config: dict[str, Any],
        trader_factory: TraderFactory,
        **values: object,
    ) -> HunterApplication:
        """Construct dependencies once; no per-trade service lookup occurs."""
        return cls(config, trader_factory(config), **values)

    def _validate_and_compose(self) -> None:
        self._transition(ApplicationState.CONFIG_VALIDATED)
        routing = routing_config_from_dict(self.config.get("execution"))
        for endpoint in routing.providers:
            if endpoint.enabled and ProviderRole.SUBMIT in endpoint.roles:
                self._provider_states[endpoint.provider_id] = ComponentStatus(
                    endpoint.provider_id,
                    RuntimeComponentState.CONFIGURED,
                    endpoint.required,
                )
        tracking = wallet_tracking_config_from_dict(self.config.get("wallet_tracking"))
        fleet_config = self.config.get("wallet_fleet")
        launch_config = self.config.get("token_launch")
        validate_wallet_fleet_config(fleet_config)
        fleet_enabled = bool(
            isinstance(fleet_config, dict) and fleet_config.get("enabled", False)
        )
        validate_token_launch_config(launch_config, wallet_fleet_enabled=fleet_enabled)
        launch_enabled = bool(
            isinstance(launch_config, dict) and launch_config.get("enabled", False)
        )
        if (
            launch_enabled
            and launch_config.get("execution", {}).get("mode", "bundle") == "bundle"
            and not any(
                endpoint.enabled
                and endpoint.capabilities.maximum_bundle_transactions is not None
                for endpoint in routing.providers
            )
        ):
            raise ValueError(
                "bundled token launch requires a bundle-capable execution provider"
            )
        if fleet_enabled or launch_enabled:
            if self._orchestration_factory is None:
                raise ValueError(
                    "enabled launch/fleet runtime requires a configured signer and orchestration factory"
                )
            launch, fleet, fleet_exit = self._orchestration_factory(
                self.config, self.trader, self.dispatch_intent
            )
            if launch_enabled and launch is None:
                raise ValueError("token launch runtime was enabled but not composed")
            if fleet_enabled and fleet is None:
                raise ValueError("wallet fleet runtime was enabled but not composed")
            self.features.token_launch_service = launch
            self.features.wallet_fleet_service = fleet
            self.features.wallet_fleet_exit_service = fleet_exit
            self._wire_orchestration_authorizers(launch, fleet_exit)
        if tracking.enabled:
            if self.trader.platform != Platform.PUMP_FUN:
                raise ValueError("tracked-wallet runtime currently requires pump_fun")
            service = TrackedWalletService(
                tracking,
                AsyncWalletEventStore(self.trader.position_store),
                self.trader.position_service,
                self._dispatch_intent_without_token,
                execute_intent_with_token=self.dispatch_intent,
            )
            self.features.tracked_wallet_service = service
            observer = self._tracked_state_changed
            if self._tracked_source_factory is not None:
                source = self._tracked_source_factory(service, observer)
            else:
                decoder = PumpFunWalletActivityDecoder(
                    IDLManager().get_parser(Platform.PUMP_FUN)
                )
                processor = TrackedWalletProcessor(
                    decoder,
                    {wallet.address for wallet in tracking.wallets},
                    service.handle,
                    maximum_pending_events=tracking.maximum_pending_events,
                    workers=tracking.decoder_workers,
                )
                source = TrackedWalletLogSource(
                    self.config["wss_endpoint"],
                    tuple(wallet.address for wallet in tracking.wallets),
                    processor,
                    state_observer=observer,
                )
            self.features.tracked_wallet_source = source
            self._listener_states["tracked_wallet"] = ComponentStatus(
                "tracked_wallet",
                RuntimeComponentState.CONFIGURED,
                required=True,
            )
        listener_name = str(self.config.get("filters", {}).get("listener_type", "logs"))
        self._listener_states[listener_name] = ComponentStatus(
            listener_name,
            RuntimeComponentState.CONFIGURED,
            required=True,
        )
        self._transition(ApplicationState.PERSISTENCE_READY)

    def _wire_orchestration_authorizers(
        self, launch: object | None, fleet_exit: object | None
    ) -> None:
        if launch is not None and hasattr(launch, "submission_authorizer"):
            launch.submission_authorizer = self._authorize_token_launch
        if fleet_exit is not None and hasattr(fleet_exit, "submission_authorizer"):
            fleet_exit.submission_authorizer = self.authorize_trade_intent

    def _compose_control_interfaces(
        self, factories: tuple[ControlInterfaceFactory, ...]
    ) -> None:
        for factory in factories:
            interface = factory(self)
            if interface.name in self._control_states:
                raise ValueError(f"duplicate control interface: {interface.name}")
            interface.set_state_observer(
                lambda state, reason, name=interface.name: self._control_state_changed(
                    name, state, reason
                )
            )
            self._control_interfaces.append(interface)
            self._control_states[interface.name] = ComponentStatus(
                interface.name,
                RuntimeComponentState.CONFIGURED,
                required=False,
            )

    async def start(self, *, start_detection: bool = True) -> None:
        """Cross the recovery barrier, warm infrastructure, then start services."""
        if self._started:
            return
        self._started = True
        await self.event_bus.start()
        self._emit("RuntimeStarted")
        try:
            await self._phase(
                ApplicationState.RECOVERY_RUNNING, self.trader.recover_runtime
            )
            self.recovery_complete = True
            await self._phase(
                ApplicationState.INFRASTRUCTURE_WARMING,
                self.trader.warm_runtime,
            )
            self._sync_provider_readiness()
            self._transition(ApplicationState.INFRASTRUCTURE_READY)
            await self._phase(
                ApplicationState.SERVICES_STARTING,
                self.trader.activate_runtime,
            )
            if not start_detection:
                self._set_primary_listener_state(RuntimeComponentState.CONNECTED)
            # Open the application gate before scheduling producers. Required
            # producer failures immediately move the state back out of READY.
            self._transition(self._ready_state())
            # Optional interfaces must never delay token detection or economic
            # recovery. They own bounded startup timeouts and report status.
            if self._control_interfaces:
                self.supervisor.create(
                    f"control-interfaces:{self.bot_id}",
                    self._start_control_interfaces(),
                    policy=TaskFailurePolicy.OPTIONAL,
                )
            source = self.features.tracked_wallet_source
            if source is not None:
                await source.start()
            self._transition(self._ready_state())
            self._emit("RuntimeReady")
            if start_detection:
                listener = next(
                    name for name in self._listener_states if name != "tracked_wallet"
                )
                self._listener_states[listener] = ComponentStatus(
                    listener,
                    RuntimeComponentState.CONNECTING,
                    required=True,
                )
                self._transition(self._ready_state())
                self.supervisor.create(
                    f"detection:{self.bot_id}",
                    self.trader.run_detection(),
                    policy=TaskFailurePolicy.CRITICAL,
                )
        except Exception as error:
            self._transition(ApplicationState.FAILED)
            self._emit("RuntimeFailed", error=type(error).__name__)
            await self.shutdown()
            raise

    async def run(self) -> None:
        """Start and wait for the primary detector to finish."""
        try:
            await self.start(start_detection=True)
            tasks = [
                item.task
                for item in self.supervisor._tasks.values()  # noqa: SLF001
                if item.name == f"detection:{self.bot_id}"
            ]
            if tasks:
                await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            return
        finally:
            await self.shutdown()

    async def dispatch_intent(
        self, intent: TradeIntent, token_info: TokenInfo | None = None
    ) -> object:
        """Gate every runtime-generated economic intent before execution."""
        self._authorize(intent)
        self._emit(
            "TradeIntentCreated",
            intent_id=intent.intent_id,
            source=intent.source.value,
            mint=str(intent.mint),
        )
        source_event = {
            TradeIntentSource.TRACKED_WALLET_CREATE: "TrackedWalletCreate",
            TradeIntentSource.TRACKED_WALLET_BUY: "TrackedWalletBuy",
        }.get(intent.source)
        if source_event is not None:
            self._emit(
                source_event,
                intent_id=intent.intent_id,
                source=intent.source.value,
                mint=str(intent.mint),
            )
        try:
            result = await self.trader.execute_intent(intent, token_info)
        except Exception as error:
            self._emit(
                "ExecutionFailed",
                intent_id=intent.intent_id,
                error=type(error).__name__,
            )
            raise
        self._emit("ExecutionConfirmed", intent_id=intent.intent_id)
        result_signature = str(getattr(result, "signature", "") or "")
        event_type = (
            "PositionOpened" if intent.action == TradeAction.BUY else "PositionClosed"
        )
        self._emit(
            event_type,
            intent_id=intent.intent_id,
            source=intent.source.value,
            mint=str(intent.mint),
            position_id=intent.position_id,
            signature=result_signature or None,
        )
        exit_event = {
            TradeIntentSource.TAKE_PROFIT: "TakeProfitTriggered",
            TradeIntentSource.STOP_LOSS: "StopLossTriggered",
            TradeIntentSource.TIMED_EXIT: "TimedExitTriggered",
            TradeIntentSource.EMERGENCY_EXIT: "EmergencyExitTriggered",
        }.get(intent.source)
        if exit_event is not None:
            self._emit(
                exit_event,
                intent_id=intent.intent_id,
                mint=str(intent.mint),
                position_id=intent.position_id,
                signature=result_signature or None,
            )
        return result

    async def _dispatch_intent_without_token(self, intent: TradeIntent) -> object:
        return await self.dispatch_intent(intent)

    def _authorize(self, intent: TradeIntent) -> None:
        self.authorize_trade_intent(intent)

    def authorize_trade_intent(self, intent: TradeIntent) -> None:
        """Authorize a typed intent while retaining managed-exit requirements."""
        self.authorize_economic_action(
            intent.source,
            intent.action,
            managed_exit=intent.position_id is not None,
        )

    def authorize_economic_action(
        self,
        source: TradeIntentSource,
        action: TradeAction,
        *,
        managed_exit: bool = False,
    ) -> None:
        """Gate exposure changes at the final application execution boundary."""
        if "deployment" in self.config:
            raise ExecutionError(
                ErrorClassification.RISK_LIMIT_EXCEEDED,
                "deployment shadow mode forbids all economic actions",
            )
        classification = classify_economic_action(source, action)
        if not self.recovery_complete:
            raise ExecutionError(
                ErrorClassification.RISK_LIMIT_EXCEEDED,
                "runtime recovery barrier is closed",
            )
        if self.state not in {ApplicationState.READY, ApplicationState.DEGRADED}:
            raise ExecutionError(
                ErrorClassification.PROVIDER_UNAVAILABLE,
                "runtime is not ready for economic execution",
            )
        if classification == EconomicActionClass.EXIT:
            if not managed_exit:
                raise ExecutionError(
                    ErrorClassification.RISK_LIMIT_EXCEEDED,
                    "defensive exit requires an existing managed position",
                )
            return
        if self.kill_switch:
            raise ExecutionError(
                ErrorClassification.RISK_LIMIT_EXCEEDED,
                "runtime kill switch blocks new exposure",
            )
        if not self.trading_enabled:
            raise ExecutionError(
                ErrorClassification.RISK_LIMIT_EXCEEDED,
                "runtime trading is disabled for new exposure",
            )

    def _authorize_token_launch(self) -> None:
        self.authorize_economic_action(
            TradeIntentSource.TOKEN_LAUNCH,
            TradeAction.LAUNCH,
        )

    async def shutdown(self, timeout_seconds: float = 15.0) -> None:
        """Stop producers first, then flush state and close owned resources."""
        async with self._shutdown_lock:
            if self.state == ApplicationState.STOPPED:
                return
            self.trading_enabled = False
            self._transition(ApplicationState.SHUTTING_DOWN)
            self._emit("RuntimeShuttingDown")
            for interface in self._control_interfaces:
                interface.stop_accepting()
            await self.supervisor.shutdown(timeout_seconds)
            source = self.features.tracked_wallet_source
            if source is not None:
                await source.close()
            await self.trader.shutdown_runtime()
            await self.event_bus.close(flush=True)
            await asyncio.gather(
                *(interface.close() for interface in self._control_interfaces),
                return_exceptions=True,
            )
            self._transition(ApplicationState.STOPPED)

    def enable_trading(self) -> None:
        """Enable entries only when runtime and configured RiskService permit it."""
        if "deployment" in self.config:
            raise ValueError("deployment shadow mode cannot enable trading")
        if self.kill_switch:
            raise ValueError("cannot enable trading while kill switch is active")
        if not self.recovery_complete:
            raise ValueError("cannot enable trading before recovery")
        self.trading_enabled = True
        self.trader.risk_service.limits.trading_enabled = True
        self._emit("TradingEnabled")

    def disable_trading(self) -> None:
        self.trading_enabled = False
        self.trader.risk_service.limits.trading_enabled = False
        self._emit("TradingDisabled")

    def activate_kill_switch(self) -> None:
        self.kill_switch = True
        self.trading_enabled = False
        self.trader.risk_service.limits.emergency_kill_switch = True
        self._emit("KillSwitchActivated")

    def list_positions(self) -> list[object]:
        return list(self.trader.position_service.list_positions())

    def list_positions_page(self, *, limit: int = 25, offset: int = 0) -> list[object]:
        """Return bounded persisted position history for operator interfaces."""
        return list(
            self.trader.position_service.list_positions_page(limit=limit, offset=offset)
        )

    def get_position(self, position_id: str) -> object:
        return self.trader.position_service.get_position(position_id)

    def get_realized_pnl(self, position_id: str) -> dict[str, int | None]:
        return self.trader.position_service.get_realized_pnl(position_id)

    def list_executions(self, limit: int = 20) -> list[object]:
        return list(self.trader.position_store.list_executions(limit))

    def list_recent_telemetry(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return bounded credential-safe telemetry through the application API."""
        return list(self.trader.position_store.list_recent_telemetry(limit))

    def get_execution(self, identity: str) -> object:
        execution = self.trader.position_store.get_execution(identity)
        if execution is None:
            execution = self.trader.position_store.get_execution_by_signature(identity)
        if execution is None:
            raise KeyError(f"unknown execution: {identity}")
        return execution

    def get_settings(self) -> dict[str, Any]:
        return dict(self.trader.position_store.get_settings())

    def operator_settings(self) -> dict[str, Any]:
        """Return an allowlisted non-secret runtime configuration summary."""
        trade = self.config.get("trade") or {}
        filters = self.config.get("filters") or {}
        execution = self.config.get("execution") or {}
        return {
            "platform": self.config.get("platform", "pump_fun"),
            "listener_type": filters.get("listener_type", "unavailable"),
            "buy_slippage": trade.get("buy_slippage"),
            "sell_slippage": trade.get("sell_slippage"),
            "exit_strategy": trade.get("exit_strategy"),
            "execution_mode": execution.get(
                "mode", execution.get("routing_mode", "standard_rpc")
            ),
        }

    def risk_settings(self) -> dict[str, Any]:
        limits = self.trader.risk_service.limits
        return {
            "enforce": limits.enforce,
            "trading_enabled": limits.trading_enabled,
            "emergency_kill_switch": limits.emergency_kill_switch,
            "maximum_buy_raw_by_quote": {
                str(key): value
                for key, value in limits.maximum_buy_raw_by_quote.items()
            },
            "maximum_position_raw_by_quote": {
                str(key): value
                for key, value in limits.maximum_position_raw_by_quote.items()
            },
            "maximum_aggregate_exposure_raw_by_quote": {
                str(key): value
                for key, value in limits.maximum_aggregate_exposure_raw_by_quote.items()
            },
            "maximum_total_transaction_fee_lamports": (
                limits.maximum_total_transaction_fee_lamports
            ),
            "maximum_priority_fee_lamports": limits.maximum_priority_fee_lamports,
            "minimum_wallet_reserve_lamports": limits.minimum_wallet_reserve_lamports,
            "maximum_trades_per_interval": limits.maximum_trades_per_interval,
            "trade_interval_seconds": limits.trade_interval_seconds,
        }

    async def wallet_balances(self) -> tuple[dict[str, object], ...]:
        balance = await self.trader.solana_client.get_wallet_balance_lamports(
            self.trader.wallet.pubkey
        )
        return (
            {
                "alias": "primary",
                "public_key": str(self.trader.wallet.pubkey),
                "lamports": balance,
                "status": "ready" if self.recovery_complete else "recovering",
            },
        )

    def prepare_manual_buy(
        self, mint: str, amount: str, quote: str | None = None
    ) -> TradeIntent:
        token_mint = Pubkey.from_string(mint)
        quote_mint = resolve_quote_mint(quote or str(WSOL_MINT))
        spend = QuoteAmountRaw.from_decimal(
            amount,
            mint=quote_mint,
            decimals=quote_decimals(quote_mint),
            rounding=RoundingDirection.DOWN,
        )
        if spend.value <= 0:
            raise ValueError("manual buy amount must be positive")
        source = TradeIntentSource.MANUAL_BUY
        return TradeIntent(
            action=TradeAction.BUY,
            source=source,
            mint=token_mint,
            wallet_id="primary",
            quote_mint=quote_mint,
            quote_amount=spend,
            token_decimals=TOKEN_DECIMALS,
            slippage=_fraction_to_bps(self.trader.buy_slippage),
            urgency=default_urgency(source),
            intent_id=f"manual-buy:{uuid4()}",
        )

    def prepare_manual_sell(self, position_id: str, percentage: str) -> TradeIntent:
        position = self.get_position(position_id)
        text = percentage.strip().removesuffix("%")
        percent = Decimal(text)
        if not percent.is_finite() or not Decimal("0") < percent <= Decimal("100"):
            raise ValueError("sell percentage must be greater than 0 and at most 100")
        percentage_bps = int(
            (percent * Decimal(100)).to_integral_value(rounding=ROUND_FLOOR)
        )
        accounting = position.accounting
        remaining = accounting.remaining_quantity_raw
        quantity = (
            remaining
            if percentage_bps == _BASIS_POINTS_SCALE
            else remaining * percentage_bps // _BASIS_POINTS_SCALE
        )
        if quantity <= 0:
            raise ValueError("sell percentage resolves to zero raw tokens")
        source = TradeIntentSource.MANUAL_SELL
        return TradeIntent(
            action=TradeAction.SELL,
            source=source,
            mint=accounting.token_mint,
            wallet_id="primary",
            quote_mint=accounting.quote_mint,
            token_amount=TokenAmountRaw(
                quantity, accounting.token_mint, accounting.token_decimals
            ),
            position_id=accounting.position_id,
            slippage=_fraction_to_bps(self.trader.sell_slippage),
            urgency=ExecutionUrgency.NORMAL,
            intent_id=f"manual-sell:{uuid4()}",
        )

    def preview_manual_trade(self, intent: TradeIntent) -> dict[str, object]:
        self.authorize_trade_intent(intent)
        if intent.action == TradeAction.SELL:
            position = self.get_position(intent.position_id or "")
            accounting = position.accounting
            if accounting.token_mint != intent.mint:
                raise ValueError("managed position mint does not match sell intent")
            if (
                intent.token_amount is None
                or intent.token_amount.value <= 0
                or intent.token_amount.value > accounting.remaining_quantity_raw
            ):
                raise ValueError("sell amount exceeds managed remaining inventory")
        else:
            positions = self.list_positions()
            existing = sum(
                item.accounting.remaining_cost_basis_raw
                for item in positions
                if item.accounting.token_mint == intent.mint
                and item.accounting.quote_mint == intent.quote_mint
            )
            aggregate = sum(
                item.accounting.remaining_cost_basis_raw
                for item in positions
                if item.accounting.quote_mint == intent.quote_mint
            )
            self.trader.risk_service.assess_entry_exposure(
                quote_mint=intent.quote_mint,
                input_raw=intent.quote_amount.value,
                existing_position_exposure_raw=existing,
                aggregate_exposure_raw=aggregate,
            )
        return {
            "intent_id": intent.intent_id,
            "source": intent.source.value,
            "mint": str(intent.mint),
            "wallet": intent.wallet_id,
            "slippage_bps": intent.slippage.value,
            "risk": "preliminary_pass; exact fees and balance revalidate at execution",
            "execution_mode": "managed runtime route",
        }

    def list_tracked_wallets(self) -> tuple[str, ...]:
        service = self.features.tracked_wallet_service
        if service is None:
            return ()
        return tuple(str(item.address) for item in service.config.wallets)

    async def submit_manual_trade(
        self, intent: TradeIntent, token_info: TokenInfo | None = None
    ) -> object:
        """Framework-neutral manual trade API for a future UI adapter."""
        if intent.source not in {
            TradeIntentSource.MANUAL_BUY,
            TradeIntentSource.MANUAL_SELL,
            TradeIntentSource.EMERGENCY_EXIT,
        }:
            raise ValueError("manual trade API requires a manual intent source")
        return await self.dispatch_intent(intent, token_info)

    def prepare_token_launch(self, request: TokenLaunchRequest) -> LaunchExecutionPlan:
        """Return a deterministic preview without signing or submission."""
        if self.features.token_launch_service is None:
            raise ValueError("token launch runtime is disabled")
        return LaunchExecutionPlan.from_request(request)

    async def submit_token_launch(
        self, request: TokenLaunchRequest, **values: object
    ) -> object:
        """Submit through the composed launch service after runtime gating."""
        self._authorize_token_launch()
        service = self.features.token_launch_service
        if service is None:
            raise ValueError("token launch runtime is disabled")
        result = await service.execute(request, **values)
        self._emit(
            "LaunchSubmitted",
            mint=str(request.mint),
            plan_id=LaunchExecutionPlan.from_request(request).plan_id,
        )
        return result

    def fleet_positions(self, plan_id: str) -> list[dict]:
        """Return persisted fleet positions through the application boundary."""
        if self.features.wallet_fleet_service is None:
            raise ValueError("wallet fleet runtime is disabled")
        return self.trader.position_store.list_fleet_positions(plan_id)

    async def submit_fleet_exit(self, **values: object) -> object:
        """Dispatch an existing fleet exit policy through the composed service."""
        self.authorize_economic_action(
            TradeIntentSource.WALLET_FLEET_EXIT,
            TradeAction.SELL,
            managed_exit=True,
        )
        service = self.features.wallet_fleet_exit_service
        if service is None:
            raise ValueError("wallet fleet exit runtime is disabled")
        result = await service.execute_exit(**values)
        self._emit("FleetExitStatus", plan_id=str(values.get("plan_id", "")))
        return result

    def runtime_status(self) -> RuntimeStatus:
        """Return a typed snapshot containing no credentials or raw clients."""
        positions = self.trader.position_service.list_positions()
        count_reader = getattr(self.trader.position_store, "runtime_counts", None)
        counts = (
            count_reader()
            if count_reader is not None
            else {
                "active_fleets": 0,
                "pending_executions": 0,
                "pending_reconciliation": sum(
                    getattr(item.accounting.status, "value", "")
                    == "reconciliation_required"
                    for item in positions
                ),
            }
        )
        blockhash_ready = None
        blockhash_cache = getattr(self.trader.solana_client, "blockhash_cache", None)
        if blockhash_cache is not None:
            blockhash_ready = blockhash_cache.current() is not None
        selection = getattr(self.trader.priority_fee_manager, "last_selection", None)
        application_ready = self.recovery_complete and self.state in {
            ApplicationState.READY,
            ApplicationState.DEGRADED,
        }
        return RuntimeStatus(
            application_state=self.state,
            application_ready=application_ready,
            trading_enabled=self.trading_enabled,
            kill_switch=self.kill_switch,
            entries_allowed=(
                application_ready
                and self.trading_enabled
                and not self.kill_switch
                and "deployment" not in self.config
            ),
            defensive_exits_allowed=application_ready
            and "deployment" not in self.config,
            recovery_complete=self.recovery_complete,
            database_ready=self.state
            not in {ApplicationState.CREATED, ApplicationState.FAILED},
            blockhash_ready=blockhash_ready,
            fee_cache_ready=selection is not None,
            tip_cache_ready=None,
            listeners=tuple(self._listener_states.values()),
            providers=tuple(self._provider_states.values()),
            control_interfaces=tuple(self._control_states.values()),
            tracked_wallet=self._listener_states.get("tracked_wallet"),
            active_bots=(self.bot_id,),
            open_positions=sum(
                getattr(item.accounting.status, "value", "") != "closed"
                for item in positions
            ),
            active_fleets=counts["active_fleets"],
            pending_executions=counts["pending_executions"],
            pending_reconciliation=counts["pending_reconciliation"],
            startup_timings=tuple(self._startup_timings),
            dropped_events=self.event_bus.dropped_events,
        )

    async def _phase(
        self, state: ApplicationState, operation: Callable[[], Awaitable[None]]
    ) -> None:
        self._transition(state)
        started = monotonic_ns()
        await operation()
        self._startup_timings.append(
            StartupPhaseTiming(state.value, (monotonic_ns() - started) / 1_000_000)
        )

    async def _start_control_interfaces(self) -> None:
        for interface in self._control_interfaces:
            self._control_states[interface.name] = ComponentStatus(
                interface.name,
                RuntimeComponentState.STARTING,
                required=False,
            )
            try:
                await interface.start()
            except Exception as error:  # noqa: BLE001
                self._control_states[interface.name] = ComponentStatus(
                    interface.name,
                    RuntimeComponentState.FAILED,
                    required=False,
                    reason=type(error).__name__,
                )
                self._emit(
                    "ControlInterfaceFailed",
                    interface=interface.name,
                    error=type(error).__name__,
                )

    def _ready_state(self) -> ApplicationState:
        ready_states = {
            RuntimeComponentState.CONNECTED,
            RuntimeComponentState.RECEIVING,
            RuntimeComponentState.READY,
            RuntimeComponentState.WARM,
        }
        required_failed = any(
            item.required and item.state not in ready_states
            for item in (
                *self._listener_states.values(),
                *self._provider_states.values(),
            )
        )
        if required_failed and not self.allow_degraded:
            return ApplicationState.NOT_READY
        if required_failed or any(
            item.state == RuntimeComponentState.DEGRADED
            for item in (
                *self._listener_states.values(),
                *self._provider_states.values(),
            )
        ):
            return ApplicationState.DEGRADED
        return ApplicationState.READY

    def _sync_provider_readiness(self) -> None:
        warmup = getattr(self.trader.solana_client, "provider_warmup_results", {})
        for name, status in tuple(self._provider_states.items()):
            ready = warmup.get(name, False)
            self._provider_states[name] = ComponentStatus(
                name,
                RuntimeComponentState.WARM if ready else RuntimeComponentState.DEGRADED,
                status.required,
                None if ready else "provider warm-up failed",
            )

    def _tracked_state_changed(self, state: str, reason: str | None) -> None:
        parsed = RuntimeComponentState(state)
        self._listener_states["tracked_wallet"] = ComponentStatus(
            "tracked_wallet", parsed, required=True, reason=reason
        )
        if parsed in {RuntimeComponentState.DEGRADED, RuntimeComponentState.FAILED}:
            self._transition(self._ready_state())
            self._emit("RuntimeDegraded", component="tracked_wallet", reason=reason)

    def _detection_state_changed(self, state: str, reason: str | None) -> None:
        self._set_primary_listener_state(RuntimeComponentState(state), reason)
        self._transition(self._ready_state())

    def _control_state_changed(self, name: str, state: str, reason: str | None) -> None:
        parsed = RuntimeComponentState(state)
        self._control_states[name] = ComponentStatus(
            name, parsed, required=False, reason=reason
        )
        if parsed in {RuntimeComponentState.DEGRADED, RuntimeComponentState.FAILED}:
            self._emit("ControlInterfaceDegraded", interface=name, reason=reason)

    def _set_primary_listener_state(
        self,
        state: RuntimeComponentState,
        reason: str | None = None,
    ) -> None:
        name = next(item for item in self._listener_states if item != "tracked_wallet")
        current = self._listener_states[name]
        self._listener_states[name] = ComponentStatus(
            name, state, current.required, reason
        )

    def _task_failed(
        self, name: str, policy: TaskFailurePolicy, error: BaseException
    ) -> None:
        if policy == TaskFailurePolicy.CRITICAL:
            self._transition(ApplicationState.NOT_READY)
        elif policy == TaskFailurePolicy.RESTARTABLE:
            self._transition(ApplicationState.DEGRADED)
        self._emit(
            "RuntimeTaskFailed",
            task=name,
            policy=policy.value,
            error=type(error).__name__,
        )

    def _transition(self, state: ApplicationState) -> None:
        self.state = state

    def _emit(self, event_type: str, **attributes: object) -> None:
        safe = {
            key: value
            for key, value in attributes.items()
            if isinstance(value, str | int | float | bool) or value is None
        }
        self.event_bus.publish_nowait(ApplicationEvent(event_type, self.bot_id, safe))


class HunterProcessRuntime:
    """Own multiple bot runtimes without sharing mutable bot state."""

    def __init__(self, applications: tuple[HunterApplication, ...]) -> None:
        names = [item.bot_id for item in applications]
        if len(set(names)) != len(names):
            raise ValueError("bot names must be unique within one process")
        self.applications = applications

    async def run(self) -> None:
        try:
            await asyncio.gather(
                *(application.run() for application in self.applications)
            )
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(application.shutdown() for application in self.applications),
            return_exceptions=True,
        )

    def runtime_status(self) -> tuple[RuntimeStatus, ...]:
        return tuple(application.runtime_status() for application in self.applications)


class RuntimeSignalCoordinator:
    """Install one process-level SIGINT/SIGTERM handler for active bot runtimes."""

    def __init__(self) -> None:
        self._applications: set[HunterApplication] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown_tasks: set[asyncio.Task[None]] = set()

    def register(self, application: HunterApplication) -> None:
        """Register a runtime and install handlers once for the current loop."""
        self._applications.add(application)
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        self._loop = loop
        for item in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(item, self._request_shutdown)
            except (NotImplementedError, RuntimeError):
                return

    def unregister(self, application: HunterApplication) -> None:
        self._applications.discard(application)

    def _request_shutdown(self) -> None:
        for application in tuple(self._applications):
            task = asyncio.create_task(
                application.shutdown(),
                name=f"signal-shutdown:{application.bot_id}",
            )
            self._shutdown_tasks.add(task)
            task.add_done_callback(self._shutdown_tasks.discard)


runtime_signal_coordinator = RuntimeSignalCoordinator()


def _fraction_to_bps(value: object) -> BasisPoints:
    fraction = Decimal(str(value))
    if not fraction.is_finite() or not Decimal(0) <= fraction <= Decimal(1):
        raise ValueError("configured slippage fraction must be between 0 and 1")
    raw = int(
        (fraction * Decimal(_BASIS_POINTS_SCALE)).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    return BasisPoints(raw)
