"""Hunter application composition root and production lifecycle boundary."""

# Composition performs explicit configuration mapping by design.
# ruff: noqa: C901, TRY003

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic_ns
from typing import Any, Protocol

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
from domain.intents import (
    EconomicActionClass,
    TradeAction,
    TradeIntent,
    TradeIntentSource,
    classify_economic_action,
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


TraderFactory = Callable[[dict[str, Any]], RuntimeTrader]
OrchestrationFactory = Callable[
    [
        dict[str, Any],
        RuntimeTrader,
        Callable[[TradeIntent, TokenInfo | None], Awaitable[object]],
    ],
    tuple[object | None, object | None, object | None],
]


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

    def __init__(
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

    async def start(self, *, start_detection: bool = True) -> None:
        """Cross the recovery barrier, warm infrastructure, then start services."""
        if self._started:
            return
        self._started = True
        await self.event_bus.start()
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
        except Exception:
            self._transition(ApplicationState.FAILED)
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
            await self.supervisor.shutdown(timeout_seconds)
            source = self.features.tracked_wallet_source
            if source is not None:
                await source.close()
            await self.trader.shutdown_runtime()
            await self.event_bus.close(flush=True)
            self._transition(ApplicationState.STOPPED)

    def enable_trading(self) -> None:
        """Enable entries only when runtime and configured RiskService permit it."""
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

    def get_position(self, position_id: str) -> object:
        return self.trader.position_service.get_position(position_id)

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
        return await service.execute(request, **values)

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
        return await service.execute_exit(**values)

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
                application_ready and self.trading_enabled and not self.kill_switch
            ),
            defensive_exits_allowed=application_ready,
            recovery_complete=self.recovery_complete,
            database_ready=self.state
            not in {ApplicationState.CREATED, ApplicationState.FAILED},
            blockhash_ready=blockhash_ready,
            fee_cache_ready=selection is not None,
            tip_cache_ready=None,
            listeners=tuple(self._listener_states.values()),
            providers=tuple(self._provider_states.values()),
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
