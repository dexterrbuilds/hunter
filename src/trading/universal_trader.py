"""
Universal trading coordinator that works with any platform.
Cleaned up to remove all platform-specific hardcoding.
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from solders.pubkey import Pubkey

from application.positions import PositionService, SolanaWalletBalanceReader
from application.risk import RiskLimits, RiskService
from cleanup.modes import (
    handle_cleanup_after_failure,
    handle_cleanup_after_sell,
    handle_cleanup_post_session,
)
from core.client import SolanaClient
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import (
    WSOL_MINT,
    normalize_quote_mint,
    quote_decimals,
    resolve_quote_amounts,
    resolve_quote_mint,
)
from core.wallet import Wallet
from domain.lifecycle import ExecutionState, PositionStatus, is_retryable
from domain.quotes import ExecutionSide
from execution.detection import detection_for
from execution.errors import ErrorClassification
from execution.providers.config import ProviderRole
from execution.providers.factory import routing_config_from_dict
from execution.telemetry_sink import AsyncTelemetrySink
from interfaces.core import Platform, TokenInfo
from monitoring.listener_factory import ListenerFactory
from monitoring.position_monitor import MonitorRetry, PositionMonitorManager
from platforms import get_platform_implementations
from storage.sqlite import SQLitePositionStore
from trading.base import TradeResult
from trading.platform_aware import PlatformAwareBuyer, PlatformAwareSeller
from trading.position import Position
from utils.logger import get_logger

# Try to use uvloop on Unix or winloop on Windows for better performance
# Fall back to standard asyncio if not available
try:
    if sys.platform == "win32":
        import winloop

        asyncio.set_event_loop_policy(winloop.EventLoopPolicy())
    else:
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    # Standard asyncio is fine, just slightly slower
    pass

logger = get_logger(__name__)

# Default for trade.max_exit_sell_attempts: how many times a tp/sl exit sell is
# re-attempted before the position is left open. A revert (slippage, curve
# moved) is not retried by the seller itself — its max_retries only covers
# transaction submission — so the retry has to happen in the monitor loop,
# where the price is re-read first. Bounded so a token that keeps reverting
# cannot pin the bot on one position forever.
DEFAULT_MAX_EXIT_SELL_ATTEMPTS = 3


def _resolve_quote_config(
    buy_amount: float,
    quote_amounts: dict[str, float] | None,
    allowed_quote_mints: list[str] | None,
) -> tuple[dict[Pubkey, float], set[Pubkey] | None]:
    """Resolve quote-asset configuration into per-mint amounts and an allowlist.

    Keys may be mint addresses or the aliases "sol"/"usdc". SOL always falls
    back to trade.buy_amount, so a config that never mentions quote assets
    keeps its existing SOL-only behaviour.

    Args:
        buy_amount: SOL amount per buy from trade.buy_amount
        quote_amounts: Optional map of quote mint -> amount in whole units
        allowed_quote_mints: Optional list of quote mints permitted to trade

    Returns:
        Tuple of (amount per quote mint, allowed quote mints or None for any)
    """
    amounts = {WSOL_MINT: buy_amount, **resolve_quote_amounts(quote_amounts)}
    allowed = (
        {resolve_quote_mint(mint) for mint in allowed_quote_mints}
        if allowed_quote_mints
        else None
    )
    return amounts, allowed


class UniversalTrader:
    """Universal trading coordinator that works with any supported platform."""

    def __init__(
        self,
        rpc_endpoint: str,
        wss_endpoint: str,
        private_key: str,
        buy_amount: float,
        buy_slippage: float,
        sell_slippage: float,
        # Platform configuration
        platform: Platform | str = Platform.PUMP_FUN,
        # Listener configuration
        listener_type: str = "logs",
        geyser_endpoint: str | None = None,
        geyser_api_token: str | None = None,
        geyser_auth_type: str = "x-token",
        pumpportal_url: str = "wss://pumpportal.fun/api/data",
        # Trading configuration
        extreme_fast_mode: bool = False,
        extreme_fast_token_amount: int = 30,
        curve_refresh_budget: float = 2.0,
        *,
        trust_create_event: bool = True,
        # Quote asset configuration (pump.fun non-SOL pairs)
        quote_amounts: dict[str, float] | None = None,
        allowed_quote_mints: list[str] | None = None,
        # Exit strategy configuration
        exit_strategy: str = "time_based",
        take_profit_percentage: float | None = None,
        stop_loss_percentage: float | None = None,
        max_hold_time: int | None = None,
        price_check_interval: int = 10,
        max_exit_sell_attempts: int = DEFAULT_MAX_EXIT_SELL_ATTEMPTS,
        # Priority fee configuration
        enable_dynamic_priority_fee: bool = False,
        enable_fixed_priority_fee: bool = True,
        fixed_priority_fee: int = 200_000,
        extra_priority_fee: float = 0.0,
        hard_cap_prior_fee: int = 200_000,
        priority_fee_strategy: str | None = None,
        priority_fee_cache_ttl_seconds: float = 5.0,
        priority_fee_refresh_interval_seconds: float = 2.0,
        # Retry and timeout settings
        max_retries: int = 3,
        wait_time_after_creation: int = 15,
        wait_time_after_buy: int = 15,
        wait_time_before_new_token: int = 15,
        max_token_age: int | float = 0.001,
        token_wait_timeout: int = 30,
        # Cleanup settings
        cleanup_mode: str = "disabled",
        cleanup_force_close_with_burn: bool = False,
        cleanup_with_priority_fee: bool = False,
        # Trading filters
        match_string: str | None = None,
        bro_address: str | None = None,
        marry_mode: bool = False,
        yolo_mode: bool = False,
        # Compute unit configuration
        compute_units: dict | None = None,
        # Node provider configuration
        max_rps: float = 25.0,
        # Durable state and bounded orchestration
        database_path: str = "data/hunter.sqlite3",
        max_concurrent_positions: int = 4,
        risk_limits: RiskLimits | None = None,
        execution_config: dict | None = None,
    ):
        """Initialize the universal trader."""
        # Core components
        routing_config = routing_config_from_dict(execution_config)
        if (
            routing_config.enabled
            and routing_config.jito_tip_lamports
            and (risk_limits is None or not risk_limits.enforce)
        ):
            raise ValueError("tipped execution requires active RiskService enforcement")
        if (
            routing_config.enabled
            and routing_config.jito_tip_lamports
            and not risk_limits.reject_unknown_base_fee
        ):
            raise ValueError("tipped execution cannot accept an unknown base fee")
        websocket_endpoints = (
            routing_config.for_role(ProviderRole.WEBSOCKET)
            if routing_config.enabled
            else ()
        )
        effective_wss_endpoint = (
            websocket_endpoints[0].endpoint if websocket_endpoints else wss_endpoint
        )
        maximum_blockhash_age_ms = (
            routing_config.maximum_blockhash_age_ms
            if routing_config.enabled
            else 60_000
        )
        self.solana_client = SolanaClient(
            rpc_endpoint,
            max_rps=max_rps,
            maximum_blockhash_age_ms=maximum_blockhash_age_ms,
        )
        if routing_config.enabled:
            self.solana_client.configure_execution_routing(
                routing_config,
                execution_variant=routing_config.execution_variant,
                jito_tip_lamports=routing_config.jito_tip_lamports,
                jito_tip_account=routing_config.jito_tip_account,
            )
        self.wallet = Wallet(private_key)
        self.priority_fee_manager = PriorityFeeManager(
            client=self.solana_client,
            enable_dynamic_fee=enable_dynamic_priority_fee,
            enable_fixed_fee=enable_fixed_priority_fee,
            fixed_fee=fixed_priority_fee,
            extra_fee=extra_priority_fee,
            hard_cap=hard_cap_prior_fee,
            strategy=priority_fee_strategy,
            cache_ttl_seconds=priority_fee_cache_ttl_seconds,
            refresh_interval_seconds=priority_fee_refresh_interval_seconds,
        )
        self.position_store = SQLitePositionStore(database_path)
        self.telemetry_sink = AsyncTelemetrySink(self.position_store)
        self.position_service = PositionService(self.position_store)
        self.risk_service = RiskService(risk_limits)
        self.max_concurrent_positions = max(1, max_concurrent_positions)
        self.position_monitor_manager = PositionMonitorManager(
            self.max_concurrent_positions
        )

        # Platform setup
        if isinstance(platform, str):
            self.platform = Platform(platform)
        else:
            self.platform = platform

        logger.info(f"Initialized Universal Trader for platform: {self.platform.value}")

        # Validate platform support
        try:
            from platforms import platform_factory

            if not platform_factory.registry.is_platform_supported(self.platform):
                raise ValueError(f"Platform {self.platform.value} is not supported")
        except Exception:
            logger.exception("Platform validation failed")
            raise

        # Get platform-specific implementations
        self.platform_implementations = get_platform_implementations(
            self.platform, self.solana_client
        )

        # Store compute unit and quote-asset configuration
        self.compute_units = compute_units or {}
        self.quote_amounts, self.allowed_quote_mints = _resolve_quote_config(
            buy_amount, quote_amounts, allowed_quote_mints
        )

        # Create platform-aware traders
        self.buyer, self.seller = (
            PlatformAwareBuyer(
                self.solana_client,
                self.wallet,
                self.priority_fee_manager,
                buy_amount,
                buy_slippage,
                max_retries,
                extreme_fast_token_amount,
                extreme_fast_mode,
                compute_units=self.compute_units,
                quote_amounts=self.quote_amounts,
                curve_refresh_budget=curve_refresh_budget,
                trust_create_event=trust_create_event,
                risk_service=self.risk_service,
                exposure_provider=self._risk_exposure,
                telemetry_recorder=self._record_telemetry,
            ),
            PlatformAwareSeller(
                self.solana_client,
                self.wallet,
                self.priority_fee_manager,
                sell_slippage,
                max_retries,
                compute_units=self.compute_units,
                risk_service=self.risk_service,
                exposure_provider=self._risk_exposure,
                telemetry_recorder=self._record_telemetry,
            ),
        )

        # Initialize the appropriate listener with platform filtering
        self.token_listener = ListenerFactory.create_listener(
            listener_type=listener_type,
            wss_endpoint=effective_wss_endpoint,
            geyser_endpoint=geyser_endpoint,
            geyser_api_token=geyser_api_token,
            geyser_auth_type=geyser_auth_type,
            pumpportal_url=pumpportal_url,
            platforms=[self.platform],  # Only listen for our platform
        )

        # Trading parameters
        self.buy_amount = buy_amount
        self.buy_slippage = buy_slippage
        self.sell_slippage = sell_slippage
        self.max_retries = max_retries
        self.extreme_fast_mode = extreme_fast_mode
        self.extreme_fast_token_amount = extreme_fast_token_amount

        # Exit strategy parameters
        self.exit_strategy = exit_strategy.lower()
        self.take_profit_percentage = take_profit_percentage
        self.stop_loss_percentage = stop_loss_percentage
        self.max_hold_time = max_hold_time
        # Both govern the position monitor loop. The attempt cap is clamped
        # because a value below 1 would mean "never even try to sell".
        self.price_check_interval, self.max_exit_sell_attempts = (
            price_check_interval,
            max(1, max_exit_sell_attempts),
        )

        # Timing parameters
        self.wait_time_after_creation = wait_time_after_creation
        self.wait_time_after_buy = wait_time_after_buy
        self.wait_time_before_new_token = wait_time_before_new_token
        self.max_token_age = max_token_age
        self.token_wait_timeout = token_wait_timeout

        # Cleanup parameters
        self.cleanup_mode = cleanup_mode
        self.cleanup_force_close_with_burn = cleanup_force_close_with_burn
        self.cleanup_with_priority_fee = cleanup_with_priority_fee

        # Trading filters/modes
        self.match_string = match_string
        self.bro_address = bro_address
        self.marry_mode = marry_mode
        self.yolo_mode = yolo_mode

        # State tracking
        self.traded_mints: set[Pubkey] = set()
        self.traded_token_programs: dict[
            str, Pubkey
        ] = {}  # Maps mint (as string) to token_program_id
        self.token_queue: asyncio.Queue = asyncio.Queue()
        self.processing: bool = False
        self.processed_tokens: set[str] = set()
        self.queued_tokens: set[str] = set()
        self.token_timestamps: dict[str, float] = {}
        self.active_position_ids: dict[str, str] = {
            str(item.accounting.token_mint): item.accounting.position_id
            for item in self.position_service.list_positions()
            if item.accounting.status != PositionStatus.CLOSED
        }
        self.pending_sell_signatures: dict[str, str] = {}

    async def _risk_exposure(
        self, token_mint: Pubkey, quote_mint: Pubkey
    ) -> tuple[int, int]:
        """Return current/aggregate raw quote cost basis for risk guards."""
        existing = 0
        aggregate = 0
        for position in self.position_service.list_positions():
            accounting = position.accounting
            if (
                accounting.status == PositionStatus.CLOSED
                or accounting.quote_mint != quote_mint
            ):
                continue
            aggregate += accounting.remaining_cost_basis_raw
            if accounting.token_mint == token_mint:
                existing += accounting.remaining_cost_basis_raw
        return existing, aggregate

    def _record_telemetry(self, telemetry) -> None:
        """Persist completed RPC telemetry after the confirmation hot path."""
        try:
            execution = self.position_store.get_execution(telemetry.execution_id)
            attempt = execution.submission_attempt if execution is not None else 1
            if self.telemetry_sink.running:
                self.telemetry_sink.record_nowait(telemetry, max(1, attempt))
            else:
                self.position_store.save_telemetry(telemetry, attempt=max(1, attempt))
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist execution telemetry")

    async def start(self) -> None:
        """Start the trading bot and listen for new tokens."""
        logger.info(f"Starting Universal Trader for {self.platform.value}")
        await self.telemetry_sink.start()
        await self.priority_fee_manager.start()
        logger.info(
            f"Match filter: {self.match_string if self.match_string else 'None'}"
        )
        logger.info(
            f"Creator filter: {self.bro_address if self.bro_address else 'None'}"
        )
        logger.info(f"Marry mode: {self.marry_mode}")
        logger.info(f"YOLO mode: {self.yolo_mode}")
        logger.info(f"Exit strategy: {self.exit_strategy}")

        if self.exit_strategy == "tp_sl":
            logger.info(
                f"Take profit: {self.take_profit_percentage * 100 if self.take_profit_percentage else 'None'}%"
            )
            logger.info(
                f"Stop loss: {self.stop_loss_percentage * 100 if self.stop_loss_percentage else 'None'}%"
            )
            logger.info(
                f"Max hold time: {self.max_hold_time if self.max_hold_time else 'None'} seconds"
            )
            logger.info(f"Max exit sell attempts: {self.max_exit_sell_attempts}")

        logger.info(f"Max token age: {self.max_token_age} seconds")

        try:
            health_resp = await self.solana_client.get_health()
            logger.info(f"RPC warm-up successful (getHealth passed: {health_resp})")
        except Exception as e:
            logger.warning(f"RPC warm-up failed: {e!s}")

        provider_warmup = await self.solana_client.warm_execution_providers()
        if provider_warmup:
            logger.info(
                "Execution provider connection warm-up: %s",
                provider_warmup,
            )

        await self.position_monitor_manager.start()
        await self._recover_positions()

        try:
            # Choose operating mode based on yolo_mode
            if not self.yolo_mode:
                # Single token mode: process one token and exit
                logger.info(
                    "Running in single token mode - will process one token and exit"
                )
                token_info = await self._wait_for_token()
                if token_info:
                    await self._handle_token(token_info)
                    logger.info("Finished processing single token. Exiting...")
                else:
                    logger.info(
                        f"No suitable token found within timeout period ({self.token_wait_timeout}s). Exiting..."
                    )
            else:
                # Continuous mode: process tokens until interrupted
                logger.info(
                    "Running in continuous mode - will process tokens until interrupted"
                )
                processor_tasks = [
                    asyncio.create_task(
                        self._process_token_queue(), name=f"hunter-worker-{index}"
                    )
                    for index in range(self.max_concurrent_positions)
                ]

                try:
                    await self.token_listener.listen_for_tokens(
                        lambda token: self._queue_token(token),
                        self.match_string,
                        self.bro_address,
                    )
                except Exception:
                    logger.exception("Token listening stopped due to error")
                finally:
                    for processor_task in processor_tasks:
                        processor_task.cancel()
                    await asyncio.gather(*processor_tasks, return_exceptions=True)

        except Exception:
            logger.exception("Trading stopped due to error")

        finally:
            await self._cleanup_resources()
            logger.info("Universal Trader has shut down")

    async def _wait_for_token(self) -> TokenInfo | None:
        """Wait for a single token to be detected."""
        # Create a one-time event to signal when a token is found
        token_found = asyncio.Event()
        found_token = None

        async def token_callback(token: TokenInfo) -> None:
            nonlocal found_token
            token_key = str(token.mint)

            # Only process if not already processed and fresh
            if token_key not in self.processed_tokens:
                # Record when the token was discovered
                self.token_timestamps[token_key] = monotonic()
                found_token = token
                self.processed_tokens.add(token_key)
                token_found.set()

        listener_task = asyncio.create_task(
            self.token_listener.listen_for_tokens(
                token_callback,
                self.match_string,
                self.bro_address,
            )
        )

        # Wait for a token with a timeout
        try:
            logger.info(
                f"Waiting for a suitable token (timeout: {self.token_wait_timeout}s)..."
            )
            await asyncio.wait_for(token_found.wait(), timeout=self.token_wait_timeout)
            logger.info(f"Found token: {found_token.symbol} ({found_token.mint})")
            return found_token
        except TimeoutError:
            logger.info(
                f"Timed out after waiting {self.token_wait_timeout}s for a token"
            )
            return None
        finally:
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass

    async def _cleanup_resources(self) -> None:
        """Perform cleanup operations before shutting down."""
        if self.traded_mints:
            try:
                logger.info(f"Cleaning up {len(self.traded_mints)} traded token(s)...")
                # Build parallel lists of mints and token_program_ids
                mints_list = list(self.traded_mints)
                token_program_ids = [
                    self.traded_token_programs.get(str(mint)) for mint in mints_list
                ]
                await handle_cleanup_post_session(
                    self.solana_client,
                    self.wallet,
                    mints_list,
                    token_program_ids,
                    self.priority_fee_manager,
                    self.cleanup_mode,
                    self.cleanup_with_priority_fee,
                    self.cleanup_force_close_with_burn,
                )
            except Exception:
                logger.exception("Error during cleanup")

        old_keys = {k for k in self.token_timestamps if k not in self.processed_tokens}
        for key in old_keys:
            self.token_timestamps.pop(key, None)

        await self.position_monitor_manager.stop()
        await self.priority_fee_manager.close()
        await self.solana_client.close()
        await self.telemetry_sink.close()
        self.position_store.close()

    async def _recover_positions(self) -> None:
        """Reconcile persisted inventory before any new token is traded."""
        open_positions = [
            item
            for item in self.position_service.list_positions()
            if item.accounting.status != PositionStatus.CLOSED
        ]
        if not open_positions:
            return
        await self._reconcile_pending_sell_executions(open_positions)
        report = await self.position_service.recover(
            owner=self.wallet.pubkey,
            balance_reader=SolanaWalletBalanceReader(self.solana_client),
        )
        logger.info(
            f"Recovered {len(report.eligible_position_ids)} eligible position(s); "
            f"{len(report.issues)} require reconciliation"
        )
        for issue in report.issues:
            logger.warning(
                f"Position {issue.position_id} requires reconciliation: {issue.reason}"
            )
        for position_id in report.eligible_position_ids:
            stored = self.position_service.get_position(position_id)
            token_info = self._token_info_from_metadata(stored.strategy_metadata)
            if token_info is None:
                self.position_store.mark_reconciliation_required(
                    position_id, "persisted token execution metadata is incomplete"
                )
                continue
            accounting = stored.accounting
            quantity = accounting.remaining_quantity_raw / 10**accounting.token_decimals
            if quantity <= 0:
                self.position_store.mark_reconciliation_required(
                    position_id, "persisted open position has zero inventory"
                )
                continue
            quote_cost = (
                accounting.remaining_cost_basis_raw / 10**accounting.quote_decimals
            )
            entry_price = quote_cost / quantity

            async def resume(
                info: TokenInfo = token_info,
                amount: float = quantity,
                price: float = entry_price,
            ) -> None:
                recovered_result = TradeResult(
                    success=True,
                    platform=info.platform,
                    amount=amount,
                    price=price,
                )
                if self.exit_strategy == "tp_sl":
                    await self._handle_tp_sl_exit(info, recovered_result)
                elif self.exit_strategy == "time_based":
                    await self._handle_time_based_exit(info, recovered_result)

            await self.position_monitor_manager.submit(position_id, resume)

    async def _reconcile_pending_sell_executions(self, positions: list) -> None:
        """Inspect persisted sell signatures before wallet reconciliation.

        A restart must never turn an accepted-but-unobserved sell into a new
        economically equivalent transaction. Confirmed signatures are applied
        from transaction effects; expired signatures may become retryable; all
        ambiguous states require operator reconciliation.
        """
        for stored in positions:
            accounting = stored.accounting
            if accounting.status not in {
                PositionStatus.SELL_SUBMITTED,
                PositionStatus.SELL_FAILED_RETRYABLE,
            }:
                continue
            execution = self.position_store.get_latest_execution(
                accounting.position_id, side="sell"
            )
            if execution is None or execution.signature is None:
                self.position_store.mark_reconciliation_required(
                    accounting.position_id,
                    "restart found a pending sell without persisted transaction identity",
                )
                continue
            try:
                observation = await self.solana_client.observe_transaction(
                    execution.signature,
                    last_valid_block_height=execution.last_valid_block_height,
                )
            except Exception as error:  # noqa: BLE001
                self.position_store.mark_reconciliation_required(
                    accounting.position_id,
                    f"persisted sell inspection failed: {type(error).__name__}",
                )
                continue
            self.position_store.update_execution(
                execution.logical_execution_id,
                state=observation.state,
                error_classification=observation.error_classification,
            )
            if observation.succeeded:
                try:
                    result = await self.solana_client.get_execution_effects(
                        logical_execution_id=execution.logical_execution_id,
                        side=ExecutionSide.SELL,
                        signature=execution.signature,
                        user=self.wallet.pubkey,
                        token_mint=accounting.token_mint,
                        quote_mint=accounting.quote_mint,
                    )
                except Exception as error:  # noqa: BLE001
                    self.position_store.mark_reconciliation_required(
                        accounting.position_id,
                        "confirmed sell effects could not be reconstructed: "
                        f"{type(error).__name__}",
                    )
                    continue
                current = self.position_service.get_position(
                    accounting.position_id
                ).accounting.status
                if current == PositionStatus.SELL_FAILED_RETRYABLE:
                    self.position_store.transition_position(
                        accounting.position_id,
                        PositionStatus.EXIT_REQUESTED,
                        "restart resumed persisted sell identity",
                    )
                    self.position_store.transition_position(
                        accounting.position_id,
                        PositionStatus.SELL_SUBMITTED,
                        execution.logical_execution_id,
                    )
                    current = PositionStatus.SELL_SUBMITTED
                if current != PositionStatus.SELL_CONFIRMED:
                    self.position_store.transition_position(
                        accounting.position_id,
                        PositionStatus.SELL_CONFIRMED,
                        execution.signature,
                    )
                self.position_service.apply_sell_execution(
                    accounting.position_id, result
                )
                if (
                    self.position_service.get_position(
                        accounting.position_id
                    ).accounting.status
                    == PositionStatus.CLOSED
                ):
                    self.active_position_ids.pop(str(accounting.token_mint), None)
            elif observation.state == ExecutionState.EXPIRED:
                current = self.position_service.get_position(
                    accounting.position_id
                ).accounting.status
                if current == PositionStatus.SELL_SUBMITTED:
                    self.position_store.transition_position(
                        accounting.position_id,
                        PositionStatus.SELL_FAILED_RETRYABLE,
                        ErrorClassification.BLOCKHASH_EXPIRED.value,
                    )
            elif observation.state == ExecutionState.FAILED_ON_CHAIN:
                current = self.position_service.get_position(
                    accounting.position_id
                ).accounting.status
                if current == PositionStatus.SELL_FAILED_RETRYABLE:
                    self.position_store.transition_position(
                        accounting.position_id,
                        PositionStatus.EXIT_REQUESTED,
                        "restart inspected prior sell signature",
                    )
                    self.position_store.transition_position(
                        accounting.position_id,
                        PositionStatus.SELL_SUBMITTED,
                        execution.logical_execution_id,
                    )
                    current = PositionStatus.SELL_SUBMITTED
                if current == PositionStatus.SELL_SUBMITTED:
                    self.position_store.transition_position(
                        accounting.position_id,
                        PositionStatus.SELL_FAILED_PERMANENT,
                        ErrorClassification.ON_CHAIN_PROGRAM_FAILURE.value,
                    )
            else:
                self.position_store.mark_reconciliation_required(
                    accounting.position_id,
                    "persisted sell signature remains accepted but not conclusively observed",
                )

    @staticmethod
    def _token_info_from_metadata(metadata: dict) -> TokenInfo | None:
        token = metadata.get("token_info")
        if not isinstance(token, dict):
            return None
        try:
            optional_pubkey = lambda value: (  # noqa: E731
                Pubkey.from_string(value) if value else None
            )
            return TokenInfo(
                name=token.get("name", token.get("symbol", "unknown")),
                symbol=token.get("symbol", "unknown"),
                uri=token.get("uri", ""),
                mint=Pubkey.from_string(token["mint"]),
                platform=Platform(token["platform"]),
                bonding_curve=optional_pubkey(token.get("bonding_curve")),
                associated_bonding_curve=optional_pubkey(
                    token.get("associated_bonding_curve")
                ),
                pool_state=optional_pubkey(token.get("pool_state")),
                base_vault=optional_pubkey(token.get("base_vault")),
                quote_vault=optional_pubkey(token.get("quote_vault")),
                creator=optional_pubkey(token.get("creator")),
                creator_vault=optional_pubkey(token.get("creator_vault")),
                token_program_id=optional_pubkey(token.get("token_program_id")),
                quote_mint=optional_pubkey(token.get("quote_mint")),
                is_mayhem_mode=bool(token.get("is_mayhem_mode", False)),
                is_cashback_coin=bool(token.get("is_cashback_coin", False)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    async def _queue_token(self, token_info: TokenInfo) -> None:
        """Queue a token for processing if not already processed."""
        token_key = str(token_info.mint)

        if token_key in self.processed_tokens or token_key in self.queued_tokens:
            logger.debug(f"Token {token_info.symbol} already processed. Skipping...")
            return

        # Record timestamp when token was discovered
        self.token_timestamps[token_key] = monotonic()
        self.queued_tokens.add(token_key)
        try:
            await self.token_queue.put(token_info)
        except BaseException:
            self.queued_tokens.discard(token_key)
            raise
        logger.info(
            f"Queued new token: {token_info.symbol} ({token_info.mint}) on {token_info.platform.value}"
        )

    async def _process_token_queue(self) -> None:
        """Continuously process tokens from the queue, only if they're fresh."""
        while True:
            received_item = False
            try:
                token_info = await self.token_queue.get()
                received_item = True
                token_key = str(token_info.mint)
                self.processed_tokens.add(token_key)
                self.queued_tokens.discard(token_key)
                detection = detection_for(token_info)
                if detection is not None:
                    detection.mark_processing_started()

                # Check if token is still "fresh"
                current_time = monotonic()
                token_age = current_time - self.token_timestamps.get(
                    token_key, current_time
                )

                if token_age > self.max_token_age:
                    logger.info(
                        f"Skipping token {token_info.symbol} - too old ({token_age:.1f}s > {self.max_token_age}s)"
                    )
                    continue

                logger.info(
                    f"Processing fresh token: {token_info.symbol} (age: {token_age:.1f}s)"
                )
                await self._handle_token(token_info)

            except asyncio.CancelledError:
                logger.info("Token queue processor was cancelled")
                break
            except Exception:
                logger.exception("Error in token queue processor")
            finally:
                if received_item:
                    self.token_queue.task_done()

    async def _handle_token(self, token_info: TokenInfo) -> None:
        """Handle a new token creation event."""
        try:
            detection = detection_for(token_info)
            if (
                detection is not None
                and detection.hunter_processing_started_mono_ns is None
            ):
                detection.mark_processing_started()
            # Validate that token is for our platform
            if token_info.platform != self.platform:
                logger.warning(
                    f"Token platform mismatch: expected {self.platform.value}, got {token_info.platform.value}"
                )
                return

            # Skip coins paired against a quote asset we are not set up to
            # trade. Cheaper to drop here than to fail a buy on-chain.
            token_quote_mint = normalize_quote_mint(token_info.quote_mint)
            if (
                self.allowed_quote_mints is not None
                and token_quote_mint not in self.allowed_quote_mints
            ):
                logger.info(
                    f"Skipping {token_info.symbol} - quote mint {token_quote_mint} "
                    f"not in allowed_quote_mints"
                )
                return
            if token_quote_mint not in self.quote_amounts:
                logger.info(
                    f"Skipping {token_info.symbol} - no buy amount configured for "
                    f"quote mint {token_quote_mint}"
                )
                return

            # Wait for pool/curve to stabilize (unless in extreme fast mode)
            if not self.extreme_fast_mode:
                await self._save_token_info(token_info)
                logger.info(
                    f"Waiting for {self.wait_time_after_creation} seconds for the pool/curve to stabilize..."
                )
                await asyncio.sleep(self.wait_time_after_creation)

            # Buy token
            logger.info(
                f"Buying {self.quote_amounts[token_quote_mint]:.6f} of quote "
                f"{token_quote_mint} worth of {token_info.symbol} "
                f"on {token_info.platform.value}..."
            )
            if detection is not None:
                detection.mark_trade_request_created()
            buy_result: TradeResult = await self._execute_managed_buy(token_info)

            if buy_result.success:
                await self._handle_successful_buy(token_info, buy_result)
            else:
                await self._handle_failed_buy(token_info, buy_result)

            # Only wait for next token in yolo mode
            if self.yolo_mode:
                logger.info(
                    f"YOLO mode enabled. Waiting {self.wait_time_before_new_token} seconds before looking for next token..."
                )
                await asyncio.sleep(self.wait_time_before_new_token)

        except Exception:
            logger.exception(f"Error handling token {token_info.symbol}")

    async def _execute_managed_buy(
        self,
        token_info: TokenInfo,
        *,
        logical_execution_id: str | None = None,
    ) -> TradeResult:
        """Persist logical buy identity before waiting for confirmation."""
        if not hasattr(self, "position_store"):
            return await self.buyer.execute(token_info)
        logical_execution_id = logical_execution_id or f"buy:{token_info.mint}"
        execution = self.position_store.create_execution(
            logical_execution_id, position_id=None, side="buy"
        )

        def record_submission(signature, blockhash_context) -> None:
            self.position_store.update_execution(
                logical_execution_id,
                state=ExecutionState.SIGNATURE_RECEIVED,
                signature=str(signature),
                blockhash=(blockhash_context.blockhash if blockhash_context else None),
                last_valid_block_height=(
                    blockhash_context.last_valid_block_height
                    if blockhash_context
                    else None
                ),
                increment_attempt=True,
            )

        result = await self.buyer.execute(
            token_info,
            existing_signature=execution.signature,
            existing_last_valid_block_height=execution.last_valid_block_height,
            logical_execution_id=logical_execution_id,
            submission_recorder=record_submission,
        )
        if result.success:
            self.position_store.update_execution(
                logical_execution_id, state=ExecutionState.CONFIRMED
            )
        else:
            observation = getattr(
                self.solana_client, "last_transaction_observation", None
            )
            if observation is not None and (
                execution.signature is not None or result.tx_signature is not None
            ):
                self.position_store.update_execution(
                    logical_execution_id,
                    state=observation.state,
                    error_classification=(
                        result.error_classification or observation.error_classification
                    ),
                )
        return result

    async def _handle_successful_buy(
        self, token_info: TokenInfo, buy_result: TradeResult
    ) -> None:
        """Handle successful token purchase."""
        logger.info(
            f"Successfully bought {token_info.symbol} on {token_info.platform.value}"
        )
        if not buy_result.reused_existing_signature:
            self.risk_service.record_trade()
        self._log_trade(
            "buy",
            token_info,
            buy_result.price,
            buy_result.amount,
            buy_result.tx_signature,
        )
        self.traded_mints.add(token_info.mint)
        # Track token program for cleanup
        mint_str = str(token_info.mint)
        if token_info.token_program_id:
            self.traded_token_programs[mint_str] = token_info.token_program_id

        buy_execution_id = (
            buy_result.execution_plan.logical_execution_id
            if buy_result.execution_plan is not None
            else f"buy:{token_info.mint}"
        )
        buy_execution = self.position_store.get_execution(buy_execution_id)
        if buy_result.execution_result is not None:
            existing_position_id = (
                buy_execution.position_id
                if buy_execution is not None and buy_execution.position_id
                else self.active_position_ids.get(mint_str)
            )
            if buy_result.reused_existing_signature and existing_position_id:
                stored = self.position_service.get_position(existing_position_id)
            else:
                stored = self.position_service.open_from_execution(
                    buy_result.execution_result,
                    token_mint=token_info.mint,
                    quote_mint=normalize_quote_mint(token_info.quote_mint),
                    token_decimals=6,
                    quote_decimals=quote_decimals(
                        normalize_quote_mint(token_info.quote_mint)
                    ),
                    strategy_metadata={
                        "symbol": token_info.symbol,
                        "platform": token_info.platform.value,
                        "exit_strategy": self.exit_strategy,
                        "token_info": self._serialize_token_info(token_info),
                    },
                )
                self.active_position_ids[mint_str] = stored.accounting.position_id
            if buy_execution is not None and buy_execution.position_id is None:
                self.position_store.attach_execution_position(
                    buy_execution_id,
                    stored.accounting.position_id,
                )
            if stored.accounting.status == PositionStatus.CLOSED:
                logger.info(
                    f"Ignoring replay of completed buy {buy_result.tx_signature}; "
                    "its persisted position is already closed"
                )
                return
            self.active_position_ids[mint_str] = stored.accounting.position_id
        else:
            logger.warning(
                f"Buy {buy_result.tx_signature} has no structured execution effects; "
                "the position cannot be safely persisted"
            )

        # Choose exit strategy. Continuous detection submits the monitor to a
        # bounded independent worker pool; single-token mode keeps its original
        # wait-until-exit behavior.
        async def monitor_exit() -> None:
            if self.exit_strategy == "tp_sl":
                await self._handle_tp_sl_exit(token_info, buy_result)
            elif self.exit_strategy == "time_based":
                await self._handle_time_based_exit(token_info, buy_result)

        if not self.marry_mode:
            if self.exit_strategy in {"tp_sl", "time_based"}:
                position_id = self.active_position_ids.get(mint_str, mint_str)
                if self.yolo_mode:
                    await self.position_monitor_manager.submit(
                        position_id, monitor_exit
                    )
                else:
                    await monitor_exit()
            elif self.exit_strategy == "manual":
                logger.info("Manual exit strategy - position will remain open")
        else:
            logger.info("Marry mode enabled. Skipping sell operation.")

    @staticmethod
    def _serialize_token_info(token_info: TokenInfo) -> dict[str, object]:
        def text(value: Pubkey | None) -> str | None:
            return str(value) if value is not None else None

        return {
            "name": token_info.name,
            "symbol": token_info.symbol,
            "uri": token_info.uri,
            "mint": str(token_info.mint),
            "platform": token_info.platform.value,
            "bonding_curve": text(token_info.bonding_curve),
            "associated_bonding_curve": text(token_info.associated_bonding_curve),
            "pool_state": text(token_info.pool_state),
            "base_vault": text(token_info.base_vault),
            "quote_vault": text(token_info.quote_vault),
            "creator": text(token_info.creator),
            "creator_vault": text(token_info.creator_vault),
            "token_program_id": text(token_info.token_program_id),
            "quote_mint": str(normalize_quote_mint(token_info.quote_mint)),
            "is_mayhem_mode": token_info.is_mayhem_mode,
            "is_cashback_coin": token_info.is_cashback_coin,
        }

    async def _handle_failed_buy(
        self, token_info: TokenInfo, buy_result: TradeResult
    ) -> None:
        """Handle failed token purchase."""
        logger.error(f"Failed to buy {token_info.symbol}: {buy_result.error_message}")
        # Close ATA if enabled
        await handle_cleanup_after_failure(
            self.solana_client,
            self.wallet,
            token_info.mint,
            token_info.token_program_id,
            self.priority_fee_manager,
            self.cleanup_mode,
            self.cleanup_with_priority_fee,
            self.cleanup_force_close_with_burn,
        )

    async def _handle_tp_sl_exit(
        self, token_info: TokenInfo, buy_result: TradeResult
    ) -> None:
        """Handle take profit/stop loss exit strategy."""
        # Create position
        position = Position.create_from_buy_result(
            mint=token_info.mint,
            symbol=token_info.symbol,
            entry_price=buy_result.price,
            quantity=buy_result.amount,
            take_profit_percentage=self.take_profit_percentage,
            stop_loss_percentage=self.stop_loss_percentage,
            max_hold_time=self.max_hold_time,
        )

        logger.info(f"Created position: {position}")
        if position.take_profit_price:
            logger.info(f"Take profit target: {position.take_profit_price:.8f} SOL")
        if position.stop_loss_price:
            logger.info(f"Stop loss target: {position.stop_loss_price:.8f} SOL")

        # Monitor position until exit condition is met
        await self._monitor_position_until_exit(token_info, position)

    async def _handle_time_based_exit(
        self, token_info: TokenInfo, buy_result: TradeResult
    ) -> None:
        """Handle legacy time-based exit strategy.

        Args:
            token_info: Token information
            buy_result: Result from the buy operation (contains token amount)
        """
        logger.info(f"Waiting for {self.wait_time_after_buy} seconds before selling...")
        await asyncio.sleep(self.wait_time_after_buy)

        logger.info(f"Selling {token_info.symbol}...")
        # Pass token amount and price from buy result to avoid RPC delays
        while True:
            sell_result = await self._execute_managed_sell(
                token_info,
                token_amount=buy_result.amount,
                token_price=buy_result.price,
            )

            if sell_result.success:
                logger.info(f"Successfully sold {token_info.symbol}")
                self._log_trade(
                    "sell",
                    token_info,
                    sell_result.price,
                    sell_result.amount,
                    sell_result.tx_signature,
                )
                await handle_cleanup_after_sell(
                    self.solana_client,
                    self.wallet,
                    token_info.mint,
                    token_info.token_program_id,
                    self.priority_fee_manager,
                    self.cleanup_mode,
                    self.cleanup_with_priority_fee,
                    self.cleanup_force_close_with_burn,
                )
                return

            logger.error(
                f"Failed to sell {token_info.symbol}: {sell_result.error_message}"
            )
            if sell_result.error_classification is not None and not is_retryable(
                sell_result.error_classification
            ):
                logger.error(
                    f"Sell for {token_info.symbol} entered an explicit permanent "
                    f"failure state: {sell_result.error_classification.value}"
                )
                return
            await asyncio.sleep(self.price_check_interval)

    async def _monitor_position_until_exit(
        self, token_info: TokenInfo, position: Position
    ) -> None:
        """Monitor a position until exit conditions are met."""
        logger.info(
            f"Starting position monitoring (check interval: {self.price_check_interval}s)"
        )

        # Get pool address for price monitoring using platform-agnostic method
        pool_address = self._get_pool_address(token_info)
        curve_manager = self.platform_implementations.curve_manager
        exit_sell_attempts = 0

        while position.is_active:
            try:
                # Get current price from pool/curve
                current_price = await curve_manager.calculate_price(pool_address)

                # Check if position should be exited
                should_exit, exit_reason = position.should_exit(current_price)

                if should_exit and exit_reason:
                    logger.info(f"Exit condition met: {exit_reason.value}")
                    logger.info(f"Current price: {current_price:.8f} SOL")

                    # Log PnL before exit
                    pnl = position.get_pnl(current_price)
                    logger.info(
                        f"Position PnL: {pnl['price_change_pct']:.2f}% ({pnl['unrealized_pnl_sol']:.6f} SOL)"
                    )

                    # Sell against the price that just triggered the exit, not
                    # the entry price: the seller turns this into the slippage
                    # floor, and by definition an exit fires once the price has
                    # moved away from entry. current_price cost no extra RPC
                    # call — it was fetched at the top of this iteration.
                    exit_sell_attempts += 1
                    sell_result = await self._execute_managed_sell(
                        token_info,
                        token_amount=position.quantity,
                        token_price=current_price,
                    )

                    if sell_result.success:
                        # Close position with actual exit price
                        position.close_position(sell_result.price, exit_reason)

                        logger.info(
                            f"Successfully exited position: {exit_reason.value}"
                        )
                        self._log_trade(
                            "sell",
                            token_info,
                            sell_result.price,
                            sell_result.amount,
                            sell_result.tx_signature,
                        )

                        # Log final PnL
                        final_pnl = position.get_pnl()
                        logger.info(
                            f"Final PnL: {final_pnl['price_change_pct']:.2f}% ({final_pnl['unrealized_pnl_sol']:.6f} SOL)"
                        )

                        # Close ATA if enabled
                        await handle_cleanup_after_sell(
                            self.solana_client,
                            self.wallet,
                            token_info.mint,
                            token_info.token_program_id,
                            self.priority_fee_manager,
                            self.cleanup_mode,
                            self.cleanup_with_priority_fee,
                            self.cleanup_force_close_with_burn,
                        )
                        break

                    logger.error(
                        f"Failed to exit position (attempt "
                        f"{exit_sell_attempts}/{self.max_exit_sell_attempts}): "
                        f"{sell_result.error_message}"
                    )
                    if (
                        sell_result.error_classification is not None
                        and not is_retryable(sell_result.error_classification)
                    ):
                        logger.error(
                            f"Position {token_info.symbol} is persisted in "
                            "SELL_FAILED_PERMANENT and requires operator review"
                        )
                        break
                    if exit_sell_attempts >= self.max_exit_sell_attempts:
                        logger.error(
                            f"Exit attempts reached {exit_sell_attempts} for "
                            f"{token_info.symbol}; monitoring remains active and "
                            "will continue after the next state/price check"
                        )
                        # The legacy loop remains bounded, but production
                        # monitors are durably requeued by the fixed worker
                        # pool instead of being forgotten.
                        if hasattr(self, "position_monitor_manager") and str(
                            token_info.mint
                        ) in getattr(self, "active_position_ids", {}):
                            raise MonitorRetry(self.price_check_interval)
                        break
                    # Keep monitoring: the next iteration re-reads the price and
                    # retries the sell with a floor that matches the market.
                else:
                    # Log current status
                    exit_sell_attempts = 0
                    pnl = position.get_pnl(current_price)
                    logger.debug(
                        f"Position status: {current_price:.8f} SOL ({pnl['price_change_pct']:+.2f}%)"
                    )

                # Wait before next price check
                await asyncio.sleep(self.price_check_interval)

            except MonitorRetry:
                raise
            except Exception:
                logger.exception("Error monitoring position")
                await asyncio.sleep(
                    self.price_check_interval
                )  # Continue monitoring despite errors

    async def _execute_managed_sell(
        self,
        token_info: TokenInfo,
        *,
        token_amount: float,
        token_price: float,
        logical_execution_id: str | None = None,
    ) -> TradeResult:
        """Persist sell lifecycle and inspect ambiguous signatures before retry."""
        if not hasattr(self, "active_position_ids"):
            # Offline Milestone 1 verification harnesses construct a minimal
            # coordinator without persistence; preserve their call contract.
            return await self.seller.execute(
                token_info, token_amount=token_amount, token_price=token_price
            )
        mint_key = str(token_info.mint)
        position_id = self.active_position_ids.get(mint_key)
        pending_execution = None
        if position_id is not None:
            stored = self.position_service.get_position(position_id)
            pending_execution = self.position_store.get_pending_execution(
                position_id, side="sell"
            )
            logical_execution_id = (
                pending_execution.logical_execution_id
                if pending_execution is not None
                else logical_execution_id
                or f"sell:{position_id}:{stored.accounting.sold_quantity_raw}"
            )
            pending_execution = self.position_store.create_execution(
                logical_execution_id, position_id=position_id, side="sell"
            )
            status = stored.accounting.status
            if status == PositionStatus.SELL_FAILED_PERMANENT:
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message="Position sell is in permanent-failure state",
                    error_classification=ErrorClassification.ON_CHAIN_PROGRAM_FAILURE,
                )
            if status in {PositionStatus.OPEN, PositionStatus.SELL_FAILED_RETRYABLE}:
                self.position_store.transition_position(
                    position_id,
                    PositionStatus.EXIT_REQUESTED,
                    "exit strategy requested sell",
                )
                status = PositionStatus.EXIT_REQUESTED
            if status == PositionStatus.EXIT_REQUESTED:
                self.position_store.transition_position(
                    position_id,
                    PositionStatus.SELL_SUBMITTED,
                    "sell construction/submission started",
                )

        def record_submission(signature, blockhash_context) -> None:
            if logical_execution_id is None:
                return
            self.position_store.update_execution(
                logical_execution_id,
                state=ExecutionState.SIGNATURE_RECEIVED,
                signature=str(signature),
                blockhash=(blockhash_context.blockhash if blockhash_context else None),
                last_valid_block_height=(
                    blockhash_context.last_valid_block_height
                    if blockhash_context
                    else None
                ),
                increment_attempt=True,
            )

        result = await self.seller.execute(
            token_info,
            token_amount=token_amount,
            token_price=token_price,
            existing_signature=(
                pending_execution.signature if pending_execution else None
            ),
            existing_last_valid_block_height=(
                pending_execution.last_valid_block_height if pending_execution else None
            ),
            logical_execution_id=logical_execution_id,
            submission_recorder=(record_submission if position_id else None),
        )
        if result.tx_signature:
            self.pending_sell_signatures[mint_key] = str(result.tx_signature)

        if position_id is None:
            return result
        if result.success and result.execution_result is not None:
            self.risk_service.record_trade()
            if logical_execution_id is not None:
                self.position_store.update_execution(
                    logical_execution_id, state=ExecutionState.CONFIRMED
                )
            current = self.position_service.get_position(position_id)
            if current.accounting.status == PositionStatus.SELL_SUBMITTED:
                self.position_store.transition_position(
                    position_id,
                    PositionStatus.SELL_CONFIRMED,
                    str(result.tx_signature),
                )
            self.position_service.apply_sell_execution(
                position_id, result.execution_result
            )
            self.pending_sell_signatures.pop(mint_key, None)
            if (
                self.position_service.get_position(position_id).accounting.status
                == PositionStatus.CLOSED
            ):
                self.active_position_ids.pop(mint_key, None)
            return result

        classification = result.error_classification or ErrorClassification.UNKNOWN
        if logical_execution_id is not None:
            observation = getattr(
                self.solana_client, "last_transaction_observation", None
            )
            execution_state = (
                observation.state
                if observation is not None
                else ExecutionState.TIMED_OUT
            )
            self.position_store.update_execution(
                logical_execution_id,
                state=execution_state,
                error_classification=classification,
            )
        current = self.position_service.get_position(position_id)
        if current.accounting.status == PositionStatus.SELL_SUBMITTED:
            target = (
                PositionStatus.SELL_FAILED_RETRYABLE
                if is_retryable(classification)
                else PositionStatus.SELL_FAILED_PERMANENT
            )
            self.position_store.transition_position(
                position_id, target, classification.value
            )
        return result

    def _get_pool_address(self, token_info: TokenInfo) -> Pubkey:
        """Get the pool/curve address for price monitoring using platform-agnostic method."""
        address_provider = self.platform_implementations.address_provider

        # Use platform-specific logic to get the appropriate address
        if hasattr(token_info, "bonding_curve") and token_info.bonding_curve:
            return token_info.bonding_curve
        elif hasattr(token_info, "pool_state") and token_info.pool_state:
            return token_info.pool_state
        else:
            # Fallback to deriving the address using platform provider
            return address_provider.derive_pool_address(token_info.mint)

    async def _save_token_info(self, token_info: TokenInfo) -> None:
        """Save token information to a file."""
        try:
            trades_dir = Path("trades")
            trades_dir.mkdir(exist_ok=True)
            file_path = trades_dir / f"{token_info.mint}.txt"

            # Convert to dictionary for saving - platform-agnostic
            token_dict = {
                "name": token_info.name,
                "symbol": token_info.symbol,
                "uri": token_info.uri,
                "mint": str(token_info.mint),
                "platform": token_info.platform.value,
                "user": str(token_info.user) if token_info.user else None,
                "creator": str(token_info.creator) if token_info.creator else None,
                "creation_timestamp": token_info.creation_timestamp,
            }

            # Add platform-specific fields only if they exist
            platform_fields = {
                "bonding_curve": token_info.bonding_curve,
                "associated_bonding_curve": token_info.associated_bonding_curve,
                "creator_vault": token_info.creator_vault,
                "pool_state": token_info.pool_state,
                "base_vault": token_info.base_vault,
                "quote_vault": token_info.quote_vault,
            }

            for field_name, field_value in platform_fields.items():
                if field_value is not None:
                    token_dict[field_name] = str(field_value)

            file_path.write_text(json.dumps(token_dict, indent=2))

            logger.info(f"Token information saved to {file_path}")
        except OSError:
            logger.exception("Failed to save token information")

    def _log_trade(
        self,
        action: str,
        token_info: TokenInfo,
        price: float,
        amount: float,
        tx_hash: str | None,
    ) -> None:
        """Log trade information."""
        try:
            trades_dir = Path("trades")
            trades_dir.mkdir(exist_ok=True)

            log_entry = {
                "timestamp": datetime.now(UTC).isoformat(),
                "action": action,
                "platform": token_info.platform.value,
                "token_address": str(token_info.mint),
                "symbol": token_info.symbol,
                "price": price,
                "amount": amount,
                "tx_hash": str(tx_hash) if tx_hash else None,
            }

            log_file_path = trades_dir / "trades.log"
            with log_file_path.open("a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(log_entry) + "\n")
        except OSError:
            logger.exception("Failed to log trade information")


# Backward compatibility alias
PumpTrader = UniversalTrader  # Legacy name for backward compatibility
