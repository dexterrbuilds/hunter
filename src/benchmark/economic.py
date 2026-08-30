"""Adapter from controlled benchmark trials to Hunter's existing buy path."""

# The executor signature mirrors the provider-neutral benchmark protocol.
# ruff: noqa: PLR0913, TC001, TRY003

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

from solders.pubkey import Pubkey

from application.risk import RiskLimits
from benchmark.live_config import LiveBenchmarkConfig
from benchmark.live_session import EconomicOutcome
from bot_runner import _risk_limits_from_config
from core.pubkeys import is_sol_paired, quote_decimals, resolve_quote_mint
from execution.detection import record_detection
from interfaces.core import Platform, TokenInfo
from trading.universal_trader import UniversalTrader


class HunterEconomicExecutor:
    """Use the production executor without starting token detection."""

    def __init__(
        self, bot_config: dict[str, Any], benchmark: LiveBenchmarkConfig
    ) -> None:
        self.config = deepcopy(bot_config)
        self.benchmark = benchmark
        self.trader: UniversalTrader | None = None
        self._last_token: TokenInfo | None = None
        self._last_buy = None

    async def execute_buy(
        self,
        *,
        mint: str,
        logical_trade_id: str,
        route_id: str,
        provider_ids: tuple[str, ...],
        mode: str,
        execution_variant: str,
    ) -> EconomicOutcome:
        self.trader = _build_trader(
            self.config,
            self.benchmark,
            provider_ids=provider_ids,
            mode=mode,
            execution_variant=execution_variant,
        )
        await self.trader.telemetry_sink.start()
        await self.trader.priority_fee_manager.start()
        if self.benchmark.warm_providers:
            await self.trader.solana_client.warm_execution_providers()
        token = TokenInfo(
            name="Hunter benchmark",
            symbol="BENCHMARK",
            uri="",
            mint=_pubkey(mint),
            platform=Platform.PUMP_FUN,
            additional_data={"benchmark_route_id": route_id},
        )
        detection = record_detection(
            token,
            source="manual_mint",
            event_slot=self.benchmark.detection_slot,
            transaction_slot=self.benchmark.authoritative_launch_slot,
            launch_slot=self.benchmark.authoritative_launch_slot,
        )
        detection.mark_processing_started()
        implementations = self.trader.platform_implementations
        refresh_error = await self.trader.buyer._refresh_curve_state(  # noqa: SLF001
            token,
            implementations.address_provider,
            implementations.curve_manager,
        )
        if refresh_error is not None:
            raise ValueError(refresh_error)
        detection.mark_trade_request_created()
        self._last_token = token
        result = await self.trader._execute_managed_buy(  # noqa: SLF001
            token,
            logical_execution_id=logical_trade_id,
        )
        if result.success:
            await self.trader._handle_successful_buy(token, result)  # noqa: SLF001
        await self.trader.telemetry_sink.flush()
        telemetry = tuple(
            item
            for item in self.trader.position_store.list_telemetry()
            if item.get("execution_id") == logical_trade_id
        )
        self._last_buy = result
        return EconomicOutcome(
            success=result.success,
            signature=str(result.tx_signature) if result.tx_signature else None,
            execution_variant=execution_variant,
            quote_spent_raw=(
                result.execution_result.quote_delta_raw
                if result.execution_result is not None
                else None
            ),
            error_classification=result.error_classification,
            error_detail=result.error_message,
            telemetry_records=telemetry,
            reused_existing_signature=result.reused_existing_signature,
        )

    async def execute_exit(
        self, *, mint: str, logical_trade_id: str
    ) -> EconomicOutcome:
        if self.trader is None or self._last_token is None or self._last_buy is None:
            raise RuntimeError("benchmark buy has not executed")
        if not self._last_buy.success:
            raise RuntimeError("cannot exit an unsuccessful benchmark buy")
        if mint != str(self._last_token.mint):
            raise ValueError("benchmark exit mint differs from acquired position")
        result = await self.trader._execute_managed_sell(  # noqa: SLF001
            self._last_token,
            token_amount=self._last_buy.amount,
            token_price=self._last_buy.price,
            logical_execution_id=logical_trade_id,
        )
        await self.trader.telemetry_sink.flush()
        telemetry = tuple(
            item
            for item in self.trader.position_store.list_telemetry()
            if str(item.get("execution_id", "")).startswith("sell:")
        )
        return EconomicOutcome(
            success=result.success,
            signature=str(result.tx_signature) if result.tx_signature else None,
            execution_variant="exit",
            quote_spent_raw=None,
            error_classification=result.error_classification,
            error_detail=result.error_message,
            telemetry_records=telemetry,
        )

    async def close(self) -> None:
        if self.trader is None:
            return
        await self.trader.priority_fee_manager.close()
        await self.trader.telemetry_sink.close()
        await self.trader.solana_client.close()
        self.trader.position_store.close()


def _build_trader(
    config: dict[str, Any],
    benchmark: LiveBenchmarkConfig,
    *,
    provider_ids: tuple[str, ...],
    mode: str,
    execution_variant: str,
) -> UniversalTrader:
    execution = deepcopy(config.get("execution") or {})
    providers = execution.get("providers", [])
    configured_ids = {
        str(item.get("id")) for item in providers if isinstance(item, dict)
    }
    unknown = set(provider_ids) - configured_ids
    if unknown:
        raise ValueError(
            f"benchmark provider IDs are not configured: {sorted(unknown)}"
        )
    for provider in providers:
        provider["enabled"] = provider.get("id") in provider_ids
    execution["enabled"] = True
    execution["mode"] = mode
    execution["execution_variant"] = execution_variant
    if execution_variant == "standard":
        execution["jito_tip_lamports"] = 0
        execution["jito_tip_account"] = None
    configured_combined = execution.get("maximum_combined_fee_lamports")
    execution["maximum_combined_fee_lamports"] = _stricter_optional(
        configured_combined,
        benchmark.caps.maximum_combined_transaction_cost_lamports,
    )
    tip = int(execution.get("jito_tip_lamports", 0))
    if tip > benchmark.caps.maximum_tip_lamports:
        raise ValueError("configured delivery tip exceeds benchmark cap")

    quote_mint = resolve_quote_mint(benchmark.quote_mint)
    decimals = quote_decimals(quote_mint)
    amount_decimal = Decimal(benchmark.quote_amount_raw) / (Decimal(10) ** decimals)
    legacy_amount = float(amount_decimal)
    if int(Decimal(str(legacy_amount)) * (Decimal(10) ** decimals)) != (
        benchmark.quote_amount_raw
    ):
        raise ValueError("benchmark amount cannot round-trip through the legacy path")
    risk_limits = _benchmark_risk_limits(
        _risk_limits_from_config(config), benchmark, quote_mint
    )
    trade = config["trade"]
    priority = config.get("priority_fees", {})
    retries = config.get("retries", {})
    filters = config["filters"]
    return UniversalTrader(
        rpc_endpoint=config["rpc_endpoint"],
        wss_endpoint=config["wss_endpoint"],
        private_key=config["private_key"],
        platform=Platform.PUMP_FUN,
        buy_amount=legacy_amount,
        buy_slippage=trade["buy_slippage"],
        sell_slippage=trade["sell_slippage"],
        extreme_fast_mode=False,
        curve_refresh_budget=trade.get("curve_refresh_budget", 2.0),
        trust_create_event=False,
        quote_amounts={str(quote_mint): legacy_amount},
        allowed_quote_mints=[str(quote_mint)],
        exit_strategy="manual",
        listener_type=filters["listener_type"],
        geyser_endpoint=config.get("geyser", {}).get("endpoint"),
        geyser_api_token=config.get("geyser", {}).get("api_token"),
        geyser_auth_type=config.get("geyser", {}).get("auth_type", "x-token"),
        pumpportal_url=config.get("pumpportal", {}).get(
            "url", "wss://pumpportal.fun/api/data"
        ),
        enable_dynamic_priority_fee=priority.get("enable_dynamic", False),
        enable_fixed_priority_fee=priority.get("enable_fixed", True),
        fixed_priority_fee=priority.get("fixed_amount", 200_000),
        extra_priority_fee=priority.get("extra_percentage", 0.0),
        hard_cap_prior_fee=priority.get("hard_cap", 200_000),
        priority_fee_strategy=priority.get("strategy"),
        priority_fee_cache_ttl_seconds=priority.get("cache_ttl_seconds", 5.0),
        priority_fee_refresh_interval_seconds=priority.get(
            "refresh_interval_seconds", 2.0
        ),
        max_retries=retries.get("max_attempts", 3),
        compute_units=config.get("compute_units", {}),
        max_rps=config.get("node", {}).get("max_rps", 25),
        database_path=config.get("storage", {}).get(
            "database_path", "data/hunter.sqlite3"
        ),
        risk_limits=risk_limits,
        execution_config=execution,
    )


def _benchmark_risk_limits(
    normal: RiskLimits, benchmark: LiveBenchmarkConfig, quote_mint: Pubkey
) -> RiskLimits:
    if not normal.enforce:
        raise PermissionError("benchmark requires active normal risk enforcement")
    maximum_buy = dict(normal.maximum_buy_raw_by_quote)
    maximum_buy[quote_mint] = _stricter_optional(
        maximum_buy.get(quote_mint), benchmark.caps.maximum_quote_amount_raw
    )
    return RiskLimits(
        enforce=True,
        trading_enabled=normal.trading_enabled,
        emergency_kill_switch=normal.emergency_kill_switch,
        maximum_buy_raw_by_quote=maximum_buy,
        maximum_position_raw_by_quote=dict(normal.maximum_position_raw_by_quote),
        maximum_aggregate_exposure_raw_by_quote=dict(
            normal.maximum_aggregate_exposure_raw_by_quote
        ),
        maximum_total_transaction_fee_lamports=_stricter_optional(
            normal.maximum_total_transaction_fee_lamports,
            benchmark.caps.maximum_combined_transaction_cost_lamports,
        ),
        maximum_priority_fee_lamports=_stricter_optional(
            normal.maximum_priority_fee_lamports,
            benchmark.caps.maximum_priority_fee_lamports,
        ),
        minimum_wallet_reserve_lamports=max(
            normal.minimum_wallet_reserve_lamports or 0,
            benchmark.caps.minimum_wallet_reserve_lamports
            + (
                benchmark.maximum_per_trade_exposure_raw - benchmark.quote_amount_raw
                if is_sol_paired(quote_mint)
                else 0
            ),
        ),
        maximum_trades_per_interval=normal.maximum_trades_per_interval,
        trade_interval_seconds=normal.trade_interval_seconds,
        reject_unknown_base_fee=True,
    )


def _stricter_optional(configured: int | None, benchmark_cap: int) -> int:
    return benchmark_cap if configured is None else min(configured, benchmark_cap)


def _pubkey(value: str) -> Pubkey:
    return Pubkey.from_string(value)
