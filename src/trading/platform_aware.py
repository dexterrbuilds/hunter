"""
Platform-aware trader implementations that use the interface system.
Final cleanup removing all platform-specific hardcoding.
"""

import asyncio
from decimal import ROUND_DOWN, Decimal
from time import monotonic, monotonic_ns
from typing import Awaitable, Callable

from solders.pubkey import Pubkey

from application.risk import FeeExposure, RiskContext, RiskService
from core.client import SolanaClient, maximum_ata_rent_lamports
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import (
    TOKEN_DECIMALS,
    WSOL_MINT,
    SystemAddresses,
    is_sol_paired,
    normalize_quote_mint,
    quote_units_per_token,
)
from core.wallet import Wallet
from domain.amounts import (
    BasisPoints,
    Lamports,
    MicroLamportsPerCU,
    QuoteAmountRaw,
    RoundingDirection,
    TokenAmountRaw,
    priority_fee_lamports_ceiling,
)
from domain.lifecycle import ExecutionState
from domain.quotes import (
    ExecutionPlan,
    ExecutionResult,
    ExecutionSide,
    quote_buy,
    quote_sell,
)
from execution.detection import detection_for
from execution.errors import ErrorClassification
from execution.telemetry import ExecutionTelemetry
from interfaces.core import AddressProvider, Platform, TokenInfo
from monitoring.performance.fast_path import assess_fast_path
from platforms import get_platform_implementations
from trading.base import Trader, TradeResult
from utils.logger import get_logger

logger = get_logger(__name__)

ExposureProvider = Callable[[Pubkey, Pubkey], Awaitable[tuple[int, int]]]
TelemetryRecorder = Callable[[ExecutionTelemetry], None]
SubmissionRecorder = Callable[[object, object | None], None]


def _apply_priority_fee_telemetry(
    telemetry: ExecutionTelemetry, manager: PriorityFeeManager
) -> None:
    """Copy the latest fee-estimate provenance into the execution record."""
    selection = getattr(manager, "last_selection", None)
    if selection is None:
        return
    telemetry.priority_fee_estimate_micro_lamports = (
        selection.estimated_micro_lamports_per_cu
    )
    telemetry.priority_fee_estimation_source = selection.source
    telemetry.priority_fee_estimate_age_ms = selection.estimate_age_ms
    telemetry.priority_fee_estimation_latency_ms = selection.estimation_latency_ms


def _apply_execution_result_telemetry(
    telemetry: ExecutionTelemetry,
    result: ExecutionResult,
    quote_mint: Pubkey,
) -> None:
    """Copy authoritative costs without mixing quote and SOL denominations."""
    telemetry.rent_lamports = result.rent_lamports
    telemetry.jito_tip_lamports = result.delivery_tip_lamports
    if (
        result.network_fee_lamports is not None
        and result.priority_fee_lamports is not None
        and result.network_fee_lamports >= result.priority_fee_lamports
    ):
        telemetry.base_network_fee_lamports = (
            result.network_fee_lamports - result.priority_fee_lamports
        )
    else:
        telemetry.base_network_fee_lamports = None
    if result.protocol_fee_raw is not None:
        telemetry.attributes["protocol_fee_raw"] = result.protocol_fee_raw
    if result.creator_fee_raw is not None:
        telemetry.attributes["creator_fee_raw"] = result.creator_fee_raw
    telemetry.attributes["fee_quote_mint"] = str(quote_mint)


def _fraction_to_basis_points(value: float) -> BasisPoints:
    """Convert a legacy decimal fraction to bps with explicit truncation."""
    decimal_bps = Decimal(str(value)) * Decimal(10_000)
    return BasisPoints(int(decimal_bps.to_integral_value(rounding=ROUND_DOWN)))


def _quote_symbol(quote_mint: Pubkey) -> str:
    """Human-readable label for a quote mint, for logging only.

    Args:
        quote_mint: Quote mint address

    Returns:
        "SOL" for wrapped SOL, otherwise a truncated mint address
    """
    if is_sol_paired(quote_mint):
        return "SOL"
    mint_str = str(quote_mint)
    return f"{mint_str[:4]}..{mint_str[-4:]}"


async def _read_pool_state_with_retry(
    curve_manager: object,
    pool_address: Pubkey,
    mint: Pubkey | None = None,
    budget_seconds: float = 2.0,
    delay_seconds: float = 0.15,
) -> tuple[dict, Pubkey | None]:
    """Read bonding curve state, retrying within a time budget on a lagging node.

    A freshly created curve may not be visible at `confirmed` yet, and a node
    can momentarily serve a slot that predates it — both surface as "account
    not found". Reading at `processed` and retrying costs a handful of RPC
    calls, which is far cheaper than trading on stale account data. Issue #170
    measured individual reads on a load-balanced endpoint lagging several
    seconds behind a fast listener, hence a time budget rather than a fixed
    attempt count.

    When `mint` is given and the curve manager supports it, the curve and the
    mint are read in one slot-consistent batch so the mint's owning token
    program comes back for free (pumpportal listeners can only guess it).

    Args:
        curve_manager: Platform curve manager
        pool_address: Bonding curve / pool address
        mint: Optional token mint to read alongside the curve
        budget_seconds: Total time to keep retrying before giving up
        delay_seconds: Pause between attempts

    Returns:
        Tuple of (decoded pool state, token program id or None if unknown)

    Raises:
        Exception: The last read error if every attempt fails
    """
    batch_read = mint is not None and hasattr(
        curve_manager, "get_pool_state_and_token_program"
    )
    deadline = monotonic() + budget_seconds
    last_error: Exception | None = None
    while True:
        try:
            if batch_read:
                result = await curve_manager.get_pool_state_and_token_program(
                    pool_address, mint, commitment="processed"
                )
            else:
                state = await curve_manager.get_pool_state(
                    pool_address, commitment="processed"
                )
                result = (state, None)
        except Exception as error:  # noqa: BLE001
            last_error = error
            if monotonic() + delay_seconds > deadline:
                break
            await asyncio.sleep(delay_seconds)
        else:
            return result

    raise last_error or RuntimeError("pool_state unavailable after retries")


async def _read_pool_and_fees_with_retry(
    curve_manager: object,
    pool_address: Pubkey,
    budget_seconds: float = 2.0,
    delay_seconds: float = 0.15,
) -> tuple[dict, object]:
    """Read Pump curve and fee state from one RPC context with bounded retry."""
    deadline = monotonic() + budget_seconds
    last_error: Exception | None = None
    while True:
        try:
            try:
                return await curve_manager.get_pool_state_and_fee_schedule(
                    pool_address, commitment="processed"
                )
            except TypeError:
                # Milestone 1 test doubles and compatible third-party curve
                # managers may expose the older one-argument contract.
                return await curve_manager.get_pool_state_and_fee_schedule(pool_address)
        except Exception as error:  # noqa: BLE001
            last_error = error
            if monotonic() + delay_seconds > deadline:
                break
            await asyncio.sleep(delay_seconds)
    raise last_error or RuntimeError("Pump curve/fee state unavailable after retries")


def _refresh_quote_mint(token_info: TokenInfo, pool_state: dict) -> Pubkey:
    """Sync token_info's quote asset from freshly-read curve state.

    Listeners do not all carry quote_mint (pumpportal carries none of the
    per-coin flags), and the curve is authoritative, so prefer its value.

    Args:
        token_info: Token information, mutated in place
        pool_state: Decoded bonding curve state

    Returns:
        The resolved quote mint
    """
    quote_mint = normalize_quote_mint(
        pool_state.get("quote_mint", token_info.quote_mint)
    )
    token_info.quote_mint = quote_mint
    return quote_mint


class PlatformAwareBuyer(Trader):
    """Platform-aware token buyer that works with any supported platform."""

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        priority_fee_manager: PriorityFeeManager,
        amount: float,
        slippage: float = 0.01,
        max_retries: int = 5,
        extreme_fast_token_amount: int = 0,
        extreme_fast_mode: bool = False,
        compute_units: dict | None = None,
        quote_amounts: dict[Pubkey, float] | None = None,
        curve_refresh_budget: float = 2.0,
        *,
        trust_create_event: bool = True,
        risk_service: RiskService | None = None,
        exposure_provider: ExposureProvider | None = None,
        telemetry_recorder: TelemetryRecorder | None = None,
    ):
        """Initialize platform-aware token buyer.

        Args:
            client: Solana RPC client
            wallet: Trading wallet
            priority_fee_manager: Priority fee strategy
            amount: Amount of SOL to spend per buy on SOL-paired coins
            slippage: Acceptable price deviation
            max_retries: Transaction submission attempts
            extreme_fast_token_amount: Tokens to buy when skipping price checks
            extreme_fast_mode: Skip curve stabilization and price check
            compute_units: Optional CU overrides
            quote_amounts: Per-quote-mint spend amounts in whole quote units,
                for coins paired against something other than SOL. A coin whose
                quote mint is absent from this map is skipped rather than
                traded with a SOL-denominated amount.
            curve_refresh_budget: Seconds to keep retrying the pre-buy curve
                read before skipping the token. A buy built without fresh curve
                state guesses fee_recipient/creator_vault and tends to revert
                on-chain (issue #170), so skipping beats racing.
            trust_create_event: Skip the pre-buy curve read entirely for
                TokenInfo marked state_from_event (creator/flags/quote_mint
                read from the on-chain CreateEvent) — extreme_fast_mode then
                makes zero RPC calls between detection and submission. Set
                False to force the refresh for every listener.
        """
        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.amount = amount
        self.slippage = slippage
        self.max_retries = max_retries
        self.extreme_fast_mode = extreme_fast_mode
        self.extreme_fast_token_amount = extreme_fast_token_amount
        self.compute_units = compute_units or {}
        self.curve_refresh_budget = curve_refresh_budget
        self.trust_create_event = trust_create_event
        self.risk_service = risk_service
        self.exposure_provider = exposure_provider
        self.telemetry_recorder = telemetry_recorder
        # SOL-paired coins always use `amount`; other quotes need an explicit
        # per-mint amount because 0.0001 USDC and 0.0001 SOL are not comparable.
        self.quote_amounts: dict[Pubkey, float] = {
            WSOL_MINT: amount,
            **(quote_amounts or {}),
        }

    def _resolve_quote_amount(self, quote_mint: Pubkey) -> float | None:
        """Get the configured spend amount for a quote mint.

        Args:
            quote_mint: Normalized quote mint

        Returns:
            Amount in whole quote units, or None if this quote is not configured
        """
        return self.quote_amounts.get(quote_mint)

    async def execute(  # noqa: PLR0913
        self,
        token_info: TokenInfo,
        *,
        existing_signature: str | None = None,
        existing_last_valid_block_height: int | None = None,
        logical_execution_id: str | None = None,
        submission_recorder: SubmissionRecorder | None = None,
        intent_source: str = "launch_snipe",
        execution_urgency: str = "high",
        quote_amount_override: QuoteAmountRaw | None = None,
        slippage_override: BasisPoints | None = None,
    ) -> TradeResult:
        """Execute buy operation using platform-specific implementations."""
        telemetry: ExecutionTelemetry | None = None
        try:
            previously_confirmed = False
            if existing_signature is not None:
                observation = await self.client.observe_transaction(
                    existing_signature,
                    last_valid_block_height=existing_last_valid_block_height,
                )
                self.client.last_transaction_observation = observation
                if observation.succeeded:
                    previously_confirmed = True
                elif observation.state in {
                    ExecutionState.NOT_OBSERVED,
                    ExecutionState.TIMED_OUT,
                    ExecutionState.SIGNATURE_RECEIVED,
                    ExecutionState.RPC_ACCEPTED,
                    ExecutionState.DROPPED_UNKNOWN,
                }:
                    return TradeResult(
                        success=False,
                        platform=token_info.platform,
                        tx_signature=existing_signature,
                        error_message="Previous buy signature remains ambiguous",
                        error_classification=(
                            observation.error_classification
                            or ErrorClassification.ACCEPTED_BUT_NOT_OBSERVED
                        ),
                    )
                elif observation.state == ExecutionState.FAILED_ON_CHAIN:
                    return TradeResult(
                        success=False,
                        platform=token_info.platform,
                        tx_signature=existing_signature,
                        error_message="Previous buy failed on chain",
                        error_classification=(
                            ErrorClassification.ON_CHAIN_PROGRAM_FAILURE
                        ),
                    )
            if not previously_confirmed:
                telemetry = ExecutionTelemetry(
                    execution_id=logical_execution_id
                    or f"buy:{token_info.mint}:{monotonic()}"
                )
                telemetry.logical_trade_id = logical_execution_id
                telemetry.apply_detection(detection_for(token_info))
                telemetry.attributes["intent_source"] = intent_source
                telemetry.attributes["execution_urgency"] = execution_urgency
            # Get platform-specific implementations
            implementations = get_platform_implementations(
                token_info.platform, self.client
            )
            address_provider = implementations.address_provider
            instruction_builder = implementations.instruction_builder
            curve_manager = implementations.curve_manager

            # Quote asset is resolved from the curve below; start from whatever
            # the listener gave us so extreme_fast_mode has a usable default.
            quote_mint = normalize_quote_mint(token_info.quote_mint)
            exact_curve = None
            exact_fee_schedule = None

            if self.extreme_fast_mode:
                # Zero-RPC hot path — the point of extreme_fast_mode. When the
                # CreateEvent already carried the canonical creator, the
                # mayhem/cashback flags and quote_mint, nothing sits between
                # detection and submission. Otherwise (pumpportal, old-format
                # events) refresh from chain or skip.
                fast_path = assess_fast_path(token_info)
                if telemetry is not None:
                    telemetry.attributes["fast_path_confidence"] = (
                        fast_path.confidence.value
                    )
                    telemetry.attributes["fast_path_missing_fields"] = ",".join(
                        fast_path.missing_fields
                    )
                    telemetry.attributes["fast_path_trust_create_event"] = (
                        self.trust_create_event
                    )
                if not self._can_skip_refresh(token_info):
                    skip_reason = await self._refresh_curve_state(
                        token_info, address_provider, curve_manager
                    )
                    if skip_reason is not None:
                        return TradeResult(
                            success=False,
                            platform=token_info.platform,
                            error_message=skip_reason,
                        )
                    quote_mint = normalize_quote_mint(token_info.quote_mint)
            else:
                # Get pool address based on platform using platform-agnostic method
                pool_address = self._get_pool_address(token_info, address_provider)

                # Regular behavior with RPC call
                # Fetch pool state to get price and mayhem mode status
                refresh_started = monotonic_ns()
                detection = detection_for(token_info)
                if detection is not None:
                    detection.authoritative_refresh_started_mono_ns = refresh_started
                if token_info.platform == Platform.PUMP_FUN and hasattr(
                    curve_manager, "get_pool_state_and_fee_schedule"
                ):
                    (
                        pool_state,
                        exact_fee_schedule,
                    ) = await _read_pool_and_fees_with_retry(
                        curve_manager,
                        pool_address,
                        budget_seconds=self.curve_refresh_budget,
                    )
                    exact_curve = curve_manager.curve_state(
                        pool_state, token_mint=token_info.mint
                    )
                else:
                    pool_state = await curve_manager.get_pool_state(pool_address)
                refresh_completed = monotonic_ns()
                if detection is not None:
                    detection.authoritative_refresh_completed_mono_ns = (
                        refresh_completed
                    )
                    detection.account_read_duration_ms = (
                        refresh_completed - refresh_started
                    ) / 1_000_000
                token_price_sol = pool_state.get("price_per_token")

                # Validate price_per_token is present and positive
                if token_price_sol is None or token_price_sol <= 0:
                    raise ValueError(
                        f"Invalid price_per_token: {token_price_sol} for pool {pool_address} "
                        f"(mint: {token_info.mint}) - cannot execute buy with zero/invalid price"
                    )

                # Set mayhem-mode and cashback flags from bonding-curve state
                # so the instruction builder picks the correct fee_recipient and
                # account-list shape (cashback sells use 17 accounts, non-cashback 16).
                token_info.is_mayhem_mode = pool_state.get("is_mayhem_mode", False)
                token_info.is_cashback_coin = pool_state.get(
                    "is_cashback_coin", token_info.is_cashback_coin
                )
                quote_mint = _refresh_quote_mint(token_info, pool_state)

            # A coin paired against a quote asset we have no configured amount
            # for cannot be traded — spending `amount` of it would be a
            # different order of magnitude entirely.
            if (
                quote_amount_override is not None
                and quote_amount_override.mint != quote_mint
            ):
                raise ValueError(  # noqa: TRY003, TRY301
                    "buy intent quote mint does not match curve quote mint"
                )
            configured_quote_amount = self._resolve_quote_amount(quote_mint)
            if quote_amount_override is None and configured_quote_amount is None:
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message=(
                        f"No configured buy amount for quote mint {quote_mint}; "
                        f"set trade.quote_amounts for this mint to trade it"
                    ),
                )

            quote_unit = quote_units_per_token(quote_mint)
            quote_label = _quote_symbol(quote_mint)
            if quote_amount_override is not None:
                quote_amount_raw = quote_amount_override.value
                quote_amount = quote_amount_raw / quote_unit
            else:
                quote_amount = configured_quote_amount
                quote_amount_raw = int(quote_amount * quote_unit)
            slippage = slippage_override or _fraction_to_basis_points(self.slippage)
            slippage_fraction = slippage.value / 10_000
            compatibility_slippage_fraction = (
                self.slippage if slippage_override is None else slippage_fraction
            )

            # Both branches need the resolved quote amount to finish sizing the
            # trade: extreme_fast_mode fixes the token count and back-derives an
            # implied price, while the regular path fixes the spend and derives
            # the token count from the curve price.
            quote_started = monotonic_ns()
            buy_quote = None
            execution_plan = None
            if self.extreme_fast_mode:
                token_amount = self.extreme_fast_token_amount
                token_price_sol = quote_amount / token_amount if token_amount > 0 else 0
            elif exact_curve is not None and exact_fee_schedule is not None:
                spend = quote_amount_override or QuoteAmountRaw.from_decimal(
                    Decimal(str(quote_amount)),
                    mint=quote_mint,
                    decimals=exact_curve.quote_decimals,
                    rounding=RoundingDirection.DOWN,
                )
                buy_quote = quote_buy(
                    spend=spend,
                    curve=exact_curve,
                    fee_rates=exact_fee_schedule.select_for_buy_budget(exact_curve),
                    trade_fee_rates=exact_fee_schedule.select(exact_curve),
                    slippage=slippage,
                )
                if buy_quote.minimum_output.value <= 0:
                    raise ValueError("Exact Pump buy quote produced zero token output")
                token_amount = float(buy_quote.expected_output.to_decimal())
                token_price_sol = quote_amount / token_amount
                minimum_token_amount_raw = buy_quote.minimum_output.value
                max_quote_amount_raw = buy_quote.maximum_input.value
                execution_plan = ExecutionPlan.for_buy(
                    buy_quote, logical_execution_id=logical_execution_id
                )
            else:
                token_amount = quote_amount / token_price_sol

            if buy_quote is None:
                # Compatibility path for LetsBonk and zero-RPC extreme-fast
                # mode. Pump's normal path above uses exact integer reserves.
                minimum_token_amount = token_amount * (
                    1 - compatibility_slippage_fraction
                )
                minimum_token_amount_raw = int(
                    minimum_token_amount * 10**TOKEN_DECIMALS
                )
                if quote_amount_override is not None or slippage_override is not None:
                    max_quote_amount_raw = (
                        quote_amount_raw * (10_000 + slippage.value) + 9_999
                    ) // 10_000
                else:
                    max_quote_amount_raw = int(
                        quote_amount
                        * quote_unit
                        * (1 + compatibility_slippage_fraction)
                    )
            if telemetry is not None:
                telemetry.attributes["quote_generation_ms"] = (
                    monotonic_ns() - quote_started
                ) / 1_000_000

            # Build buy instructions using platform-specific builder
            instruction_build_started = monotonic_ns()
            instructions = await instruction_builder.build_buy_instruction(
                token_info,
                self.wallet.pubkey,
                max_quote_amount_raw,  # amount_in (raw quote units)
                minimum_token_amount_raw,  # minimum_amount_out (tokens)
                address_provider,
            )
            if telemetry is not None:
                telemetry.attributes["instruction_construction_ms"] = (
                    monotonic_ns() - instruction_build_started
                ) / 1_000_000
                telemetry.maximum_rent_exposure_lamports = maximum_ata_rent_lamports(
                    instructions
                )

            # Get accounts for priority fee calculation
            priority_accounts = instruction_builder.get_required_accounts_for_buy(
                token_info, self.wallet.pubkey, address_provider
            )

            logger.info(
                f"Buying {token_amount:.6f} tokens at {token_price_sol:.8f} "
                f"{quote_label} per token on {token_info.platform.value}"
            )
            logger.info(
                f"Total cost: {quote_amount:.6f} {quote_label} "
                f"(max: {max_quote_amount_raw / quote_unit:.6f} {quote_label})"
            )

            priority_fee = await self.priority_fee_manager.calculate_priority_fee(
                priority_accounts
            )
            if telemetry is not None:
                _apply_priority_fee_telemetry(telemetry, self.priority_fee_manager)
            compute_unit_limit = instruction_builder.get_buy_compute_unit_limit(
                self._get_cu_override("buy", token_info.platform)
            )
            if not previously_confirmed:
                await self._assess_risk(
                    execution_plan,
                    instructions,
                    priority_fee,
                    compute_unit_limit,
                )

            # Send transaction
            if previously_confirmed:
                tx_signature = existing_signature
                success = True
                telemetry = None
            else:
                if telemetry is None:
                    raise RuntimeError("buy execution telemetry was not initialized")
                if execution_plan is not None:
                    telemetry.execution_id = execution_plan.logical_execution_id
                telemetry.apply_detection(detection_for(token_info))
                send_options = {
                    "skip_preflight": True,
                    "max_retries": self.max_retries,
                    "priority_fee": priority_fee,
                    "compute_unit_limit": compute_unit_limit,
                    "account_data_size_limit": self._get_cu_override(
                        "account_data_size", token_info.platform
                    ),
                    "telemetry": telemetry,
                }
                if submission_recorder is not None:
                    send_options["submission_callback"] = submission_recorder
                tx_signature = await self.client.build_and_send_transaction(
                    instructions, self.wallet.keypair, **send_options
                )

                success = await self.client.confirm_transaction(tx_signature)
                if not success:
                    observation = getattr(
                        self.client, "last_transaction_observation", None
                    )
                    if observation is not None:
                        telemetry.error_classification = (
                            observation.error_classification
                        )
                if not success and self.telemetry_recorder is not None:
                    self.telemetry_recorder(telemetry)

            if success:
                logger.info(f"Buy transaction confirmed: {tx_signature}")

                actual_execution = None
                if token_info.platform == Platform.PUMP_FUN:
                    if execution_plan is not None:
                        actual_execution = await self.client.get_execution_result(
                            execution_plan, tx_signature, self.wallet.pubkey
                        )
                    else:
                        actual_execution = await self.client.get_execution_effects(
                            logical_execution_id=str(tx_signature),
                            side=ExecutionSide.BUY,
                            signature=tx_signature,
                            user=self.wallet.pubkey,
                            token_mint=token_info.mint,
                            quote_mint=quote_mint,
                        )
                    if (
                        actual_execution.token_delta_raw is None
                        or actual_execution.quote_delta_raw is None
                    ):
                        raise ValueError(
                            "Confirmed Pump buy lacks authoritative balance/event effects"
                        )
                    tokens_raw = actual_execution.token_delta_raw
                    quote_spent = actual_execution.quote_delta_raw
                    if telemetry is not None:
                        _apply_execution_result_telemetry(
                            telemetry, actual_execution, quote_mint
                        )
                else:
                    tokens_raw = None
                    quote_spent = None

                # Fetch actual tokens and SOL spent from transaction
                # Uses preBalances/postBalances to get exact amounts
                if tokens_raw is None or quote_spent is None:
                    sol_destination = self._get_sol_destination(
                        token_info, address_provider
                    )
                    (
                        tokens_raw,
                        quote_spent,
                    ) = await self.client.get_buy_transaction_details(
                        str(tx_signature),
                        token_info.mint,
                        sol_destination,
                        quote_mint=quote_mint,
                    )

                if tokens_raw is not None and quote_spent is not None:
                    actual_amount = tokens_raw / 10**TOKEN_DECIMALS
                    actual_price = (quote_spent / quote_unit) / actual_amount
                    logger.info(
                        f"Actual tokens received: {actual_amount:.6f} "
                        f"(expected: {token_amount:.6f})"
                    )
                    logger.info(
                        f"Actual {quote_label} spent: "
                        f"{quote_spent / quote_unit:.10f} {quote_label}"
                    )
                    logger.info(
                        f"Actual price: {actual_price:.10f} {quote_label}/token"
                    )
                    token_amount = actual_amount
                    token_price_sol = actual_price
                else:
                    raise ValueError(
                        f"Failed to parse transaction details: tokens={tokens_raw}, "
                        f"quote_spent={quote_spent} (tx: {tx_signature}). "
                        f"The transaction may have failed on-chain — check explorer."
                    )

                if telemetry is not None and self.telemetry_recorder is not None:
                    self.telemetry_recorder(telemetry)

                return TradeResult(
                    success=True,
                    platform=token_info.platform,
                    tx_signature=tx_signature,
                    amount=token_amount,
                    price=token_price_sol,
                    quote=buy_quote,
                    execution_plan=execution_plan,
                    execution_result=actual_execution,
                    reused_existing_signature=previously_confirmed,
                )
            else:
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message=f"Transaction failed to confirm: {tx_signature}",
                )

        except Exception as e:
            failed_telemetry = locals().get("telemetry")
            if isinstance(failed_telemetry, ExecutionTelemetry):
                failed_telemetry.error_classification = (
                    e.classification
                    if hasattr(e, "classification")
                    else ErrorClassification.UNKNOWN
                )
                if self.telemetry_recorder is not None:
                    self.telemetry_recorder(failed_telemetry)
            logger.exception("Buy operation failed")
            return TradeResult(
                success=False, platform=token_info.platform, error_message=str(e)
            )

    async def _assess_risk(
        self,
        plan: ExecutionPlan | None,
        instructions: list,
        priority_fee_micro_lamports: int,
        compute_unit_limit: int,
    ) -> None:
        if self.risk_service is None or not self.risk_service.limits.enforce:
            return
        if plan is None:
            raise ValueError(
                "risk enforcement requires an exact execution plan; "
                "disable extreme-fast/legacy sizing"
            )
        base_fee = await self.client.estimate_base_fee_lamports(
            instructions, self.wallet.pubkey
        )
        priority_fee = priority_fee_lamports_ceiling(
            MicroLamportsPerCU(priority_fee_micro_lamports), compute_unit_limit
        )
        rent = Lamports(maximum_ata_rent_lamports(instructions))
        existing, aggregate = (
            await self.exposure_provider(plan.token_mint, plan.quote_mint)
            if self.exposure_provider is not None
            else (0, 0)
        )
        self.risk_service.assess(
            plan,
            RiskContext(
                wallet_lamports=await self.client.get_wallet_balance_lamports(
                    self.wallet.pubkey
                ),
                existing_position_exposure_raw=existing,
                aggregate_exposure_raw=aggregate,
                fee_exposure=FeeExposure(
                    Lamports(base_fee) if base_fee is not None else None,
                    priority_fee,
                    rent,
                    jito_tip=Lamports(getattr(self.client, "jito_tip_lamports", 0)),
                ),
                native_trade_spend_lamports=(
                    plan.input_raw if is_sol_paired(plan.quote_mint) else 0
                ),
            ),
        )

    def _get_pool_address(
        self, token_info: TokenInfo, address_provider: AddressProvider
    ) -> Pubkey:
        """Get the pool/curve address for price calculations using platform-agnostic method."""
        # Try to get the address from token_info first, then derive if needed
        if token_info.platform == Platform.PUMP_FUN:
            if hasattr(token_info, "bonding_curve") and token_info.bonding_curve:
                return token_info.bonding_curve
        elif token_info.platform == Platform.LETS_BONK:
            if hasattr(token_info, "pool_state") and token_info.pool_state:
                return token_info.pool_state

        # Fallback to deriving the address using platform provider
        return address_provider.derive_pool_address(token_info.mint)

    def _can_skip_refresh(self, token_info: TokenInfo) -> bool:
        """Whether the pre-buy curve read can be skipped entirely.

        True when the listener read creator, mayhem/cashback and quote_mint
        from the on-chain CreateEvent (canonical at create time), keeping
        extreme_fast_mode at zero RPC calls between detection and submission.

        Args:
            token_info: Token information from the listener

        Returns:
            True if the buy can be built from token_info as-is
        """
        return (
            self.trust_create_event
            and assess_fast_path(token_info).may_skip_hot_path_reads
        )

    async def _refresh_curve_state(
        self,
        token_info: TokenInfo,
        address_provider: AddressProvider,
        curve_manager: object,
    ) -> str | None:
        """Refresh mayhem/cashback/creator/quote_mint/token program from chain.

        Listeners that guess these (pumpportal carries none of them) produce
        buys the program rejects with NotAuthorized (0x1770) / ConstraintSeeds
        (0x7d6) when fee_recipient or creator_vault is wrong. PumpPortal also
        notifies before the BC account is readable on a lagging node, so the
        read retries within curve_refresh_budget.

        Args:
            token_info: Token information, mutated in place on success
            address_provider: Platform address provider
            curve_manager: Platform curve manager

        Returns:
            None on success; on failure a reason to skip the buy — a buy built
            from listener-guessed defaults tends to revert on-chain
            (issue #170: 0x1770 / 0x7d6 / pool 3012), which still costs the fee
        """
        detection = detection_for(token_info)
        refresh_started = monotonic_ns()
        if detection is not None:
            detection.authoritative_refresh_started_mono_ns = refresh_started
        try:
            pool_address = self._get_pool_address(token_info, address_provider)
            # Geyser/logs fire on processed, so the BC is typically readable in
            # the same slot; pumpportal occasionally races the on-chain commit,
            # hence the retries.
            pool_state, fresh_token_program = await _read_pool_state_with_retry(
                curve_manager,
                pool_address,
                mint=token_info.mint,
                budget_seconds=self.curve_refresh_budget,
            )
        except Exception as e:  # noqa: BLE001
            refresh_completed = monotonic_ns()
            if detection is not None:
                detection.authoritative_refresh_completed_mono_ns = refresh_completed
                detection.account_read_duration_ms = (
                    refresh_completed - refresh_started
                ) / 1_000_000
            return (
                f"Curve state unreadable within {self.curve_refresh_budget:.1f}s "
                f"({e}); skipping buy rather than submitting with guessed accounts"
            )

        refresh_completed = monotonic_ns()
        if detection is not None:
            detection.authoritative_refresh_completed_mono_ns = refresh_completed
            detection.account_read_duration_ms = (
                refresh_completed - refresh_started
            ) / 1_000_000

        token_info.is_mayhem_mode = pool_state.get(
            "is_mayhem_mode", token_info.is_mayhem_mode
        )
        token_info.is_cashback_coin = pool_state.get(
            "is_cashback_coin", token_info.is_cashback_coin
        )
        # The quote asset decides which balance we spend and how amounts are
        # scaled, so it must come from the curve rather than a listener guess.
        _refresh_quote_mint(token_info, pool_state)
        fresh_creator = pool_state.get("creator")
        if fresh_creator and hasattr(address_provider, "derive_creator_vault"):
            new_creator = (
                Pubkey.from_string(fresh_creator)
                if isinstance(fresh_creator, str)
                else fresh_creator
            )
            token_info.creator = new_creator
            token_info.creator_vault = address_provider.derive_creator_vault(
                new_creator
            )
        self._apply_token_program(token_info, fresh_token_program, address_provider)
        return None

    def _apply_token_program(
        self,
        token_info: TokenInfo,
        token_program: Pubkey | None,
        address_provider: AddressProvider,
    ) -> None:
        """Correct a listener-guessed token program from the mint's real owner.

        PumpPortal payloads carry no token program, so the processor defaults
        to Token-2022; a legacy-`create` coin is SPL Token and the ATA-create
        instruction then fails with IncorrectProgramId. The associated bonding
        curve is an ordinary ATA, so it must be re-derived under the corrected
        program too.

        Args:
            token_info: Token information, mutated in place
            token_program: Owner of the mint account, or None if unknown
            address_provider: Platform address provider for ATA derivation
        """
        known_programs = (
            SystemAddresses.TOKEN_PROGRAM,
            SystemAddresses.TOKEN_2022_PROGRAM,
        )
        if token_program is None or token_program not in known_programs:
            return
        if token_info.token_program_id == token_program:
            return
        logger.info(
            f"Correcting token program for {token_info.mint}: "
            f"{token_info.token_program_id} -> {token_program}"
        )
        token_info.token_program_id = token_program
        if token_info.bonding_curve and hasattr(
            address_provider, "derive_associated_bonding_curve"
        ):
            token_info.associated_bonding_curve = (
                address_provider.derive_associated_bonding_curve(
                    token_info.mint, token_info.bonding_curve, token_program
                )
            )

    def _get_sol_destination(
        self, token_info: TokenInfo, address_provider: AddressProvider
    ) -> Pubkey:
        """Get the address where SOL is sent during a buy transaction.

        For pump.fun: SOL goes to the bonding curve
        For letsbonk: SOL goes to the quote_vault (WSOL vault)

        Args:
            token_info: Token information
            address_provider: Platform-specific address provider

        Returns:
            Address where SOL is transferred during buy

        Raises:
            NotImplementedError: If platform SOL destination is not implemented
        """
        if token_info.platform == Platform.PUMP_FUN:
            # For pump.fun, SOL goes directly to bonding curve
            if hasattr(token_info, "bonding_curve") and token_info.bonding_curve:
                return token_info.bonding_curve
            return address_provider.derive_pool_address(token_info.mint)
        elif token_info.platform == Platform.LETS_BONK:
            # For letsbonk, SOL goes to quote_vault (WSOL vault)
            if hasattr(token_info, "quote_vault") and token_info.quote_vault:
                return token_info.quote_vault
            # Derive quote_vault if not available
            return address_provider.derive_quote_vault(token_info.mint)

        raise NotImplementedError(
            f"SOL destination not implemented for platform {token_info.platform.value}. "
            f"Add platform-specific logic to _get_sol_destination() to specify where "
            f"SOL is transferred during buy transactions for this platform."
        )

    def _get_cu_override(self, operation: str, platform: Platform) -> int | None:
        """Get compute unit override from configuration.

        Args:
            operation: "buy" or "sell"
            platform: Trading platform (unused - each config is platform-specific)

        Returns:
            CU override value if configured, None otherwise
        """
        if not self.compute_units:
            return None

        # Just check for operation override (buy/sell)
        return self.compute_units.get(operation)


class PlatformAwareSeller(Trader):
    """Platform-aware token seller that works with any supported platform."""

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        priority_fee_manager: PriorityFeeManager,
        slippage: float = 0.25,
        max_retries: int = 5,
        compute_units: dict | None = None,
        risk_service: RiskService | None = None,
        exposure_provider: ExposureProvider | None = None,
        telemetry_recorder: TelemetryRecorder | None = None,
    ):
        """Initialize platform-aware token seller."""
        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.slippage = slippage
        self.max_retries = max_retries
        self.compute_units = compute_units or {}
        self.risk_service = risk_service
        self.exposure_provider = exposure_provider
        self.telemetry_recorder = telemetry_recorder

    async def execute(
        self,
        token_info: TokenInfo,
        token_amount: float,
        token_price: float,
        *,
        existing_signature: str | None = None,
        existing_last_valid_block_height: int | None = None,
        logical_execution_id: str | None = None,
        submission_recorder: SubmissionRecorder | None = None,
        intent_source: str = "manual_sell",
        execution_urgency: str = "normal",
        token_amount_override: TokenAmountRaw | None = None,
        slippage_override: BasisPoints | None = None,
    ) -> TradeResult:
        """Execute sell operation using platform-specific implementations.

        Args:
            token_info: Token information for the sell operation
            token_amount: Token amount to sell (from buy result). Required to avoid
                         RPC balance query delays.
            token_price: Reference price in the quote asset that the slippage
                        floor is computed from. Required rather than read here,
                        to avoid RPC pool state query delays — pass the freshest
                        price the caller has. A stale price that is above the
                        market sets a floor the pool cannot pay and the sell
                        reverts (pump.fun 6003 TooLittleSolReceived).

        Returns:
            TradeResult with operation outcome

        Raises:
            ValueError: If required parameters are not provided
        """
        if token_amount is None:
            raise ValueError(
                "token_amount is required for sell operation. "
                "Pass the amount from buy result to avoid RPC delays."
            )
        if token_price is None or token_price <= 0:
            raise ValueError(
                "token_price is required for sell operation and must be positive. "
                "Pass the price from buy result to avoid RPC delays."
            )

        telemetry: ExecutionTelemetry | None = None
        try:
            previously_confirmed = False
            if existing_signature is not None:
                observation = await self.client.observe_transaction(
                    existing_signature,
                    last_valid_block_height=existing_last_valid_block_height,
                )
                self.client.last_transaction_observation = observation
                if observation.succeeded:
                    previously_confirmed = True
                elif observation.state in {
                    ExecutionState.NOT_OBSERVED,
                    ExecutionState.TIMED_OUT,
                    ExecutionState.SIGNATURE_RECEIVED,
                    ExecutionState.RPC_ACCEPTED,
                    ExecutionState.DROPPED_UNKNOWN,
                }:
                    return TradeResult(
                        success=False,
                        platform=token_info.platform,
                        tx_signature=existing_signature,
                        error_message="Previous sell signature remains ambiguous",
                        error_classification=(
                            observation.error_classification
                            or ErrorClassification.ACCEPTED_BUT_NOT_OBSERVED
                        ),
                    )
                elif observation.state == ExecutionState.FAILED_ON_CHAIN:
                    return TradeResult(
                        success=False,
                        platform=token_info.platform,
                        tx_signature=existing_signature,
                        error_message="Previous sell failed on chain",
                        error_classification=ErrorClassification.ON_CHAIN_PROGRAM_FAILURE,
                    )
            if not previously_confirmed:
                telemetry = ExecutionTelemetry(
                    execution_id=logical_execution_id
                    or f"sell:{token_info.mint}:{monotonic()}"
                )
                telemetry.logical_trade_id = logical_execution_id
                telemetry.apply_detection(detection_for(token_info))
                telemetry.attributes["intent_source"] = intent_source
                telemetry.attributes["execution_urgency"] = execution_urgency

            # Get platform-specific implementations
            implementations = get_platform_implementations(
                token_info.platform, self.client
            )
            address_provider = implementations.address_provider
            instruction_builder = implementations.instruction_builder
            curve_manager = implementations.curve_manager

            # Fall back to the listener's quote asset if the refresh below fails.
            quote_mint = normalize_quote_mint(token_info.quote_mint)
            pool_state = None
            exact_fee_schedule = None

            # Refresh mayhem-mode and cashback flags from curve state.
            # The sell account list is 16 (non-cashback) vs 17 (cashback), and
            # fee_recipient differs in mayhem mode — both can change between
            # buy and sell, so re-read from chain instead of trusting create-time
            # flags carried in token_info.
            refresh_started = monotonic_ns()
            try:
                pool_address = self._get_pool_address(token_info, address_provider)
                # Retry rather than reading once at `confirmed`: a node serving a
                # slightly stale slot reports the curve as missing, and silently
                # falling back to create-time values risks a wrong creator_vault
                # (ConstraintSeeds 0x7d6) or wrong mayhem fee_recipient.
                if token_info.platform == Platform.PUMP_FUN and hasattr(
                    curve_manager, "get_pool_state_and_fee_schedule"
                ):
                    (
                        pool_state,
                        exact_fee_schedule,
                    ) = await _read_pool_and_fees_with_retry(
                        curve_manager, pool_address
                    )
                else:
                    pool_state, _ = await _read_pool_state_with_retry(
                        curve_manager, pool_address
                    )
                token_info.is_mayhem_mode = pool_state.get(
                    "is_mayhem_mode", token_info.is_mayhem_mode
                )
                token_info.is_cashback_coin = pool_state.get(
                    "is_cashback_coin", token_info.is_cashback_coin
                )
                quote_mint = _refresh_quote_mint(token_info, pool_state)
                # Refresh creator/creator_vault from current BC state. Post
                # 2026-04-28 the program may delegate BC.creator to a PFEE-owned
                # PDA after the initial creator buy, so the create-time vault
                # cached on token_info goes stale before the sell lands. Failing
                # to refresh manifests as ConstraintSeeds (0x7d6) on Sell.
                fresh_creator = pool_state.get("creator")
                if fresh_creator:
                    from solders.pubkey import Pubkey as _Pubkey

                    new_creator = (
                        _Pubkey.from_string(fresh_creator)
                        if isinstance(fresh_creator, str)
                        else fresh_creator
                    )
                    token_info.creator = new_creator
                    token_info.creator_vault = address_provider.derive_creator_vault(
                        new_creator
                    )
            except Exception as e:  # noqa: BLE001
                if token_info.platform == Platform.PUMP_FUN and hasattr(
                    curve_manager, "curve_state"
                ):
                    raise ValueError(
                        "Current Pump curve/fee state is required for a safe sell"
                    ) from e
                logger.warning(
                    f"Could not refresh curve flags before sell ({e}); "
                    f"using token_info values is_mayhem_mode={token_info.is_mayhem_mode}, "
                    f"is_cashback_coin={token_info.is_cashback_coin}"
                )
            finally:
                if telemetry is not None:
                    telemetry.attributes["account_state_refresh_ms"] = (
                        monotonic_ns() - refresh_started
                    ) / 1_000_000

            quote_unit = quote_units_per_token(quote_mint)
            quote_label = _quote_symbol(quote_mint)

            # Use pre-known amount and price (no RPC delay)
            if (
                token_amount_override is not None
                and token_amount_override.mint != token_info.mint
            ):
                raise ValueError(  # noqa: TRY003, TRY301
                    "sell intent token mint does not match position mint"
                )
            if token_amount_override is not None:
                token_balance = token_amount_override.value
                token_balance_decimal = token_balance / 10**TOKEN_DECIMALS
            else:
                token_balance = int(token_amount * 10**TOKEN_DECIMALS)
                token_balance_decimal = token_amount
            token_price_sol = token_price
            slippage = slippage_override or _fraction_to_basis_points(self.slippage)
            slippage_fraction = slippage.value / 10_000
            compatibility_slippage_fraction = (
                self.slippage if slippage_override is None else slippage_fraction
            )
            sell_quote = None
            execution_plan = None

            logger.info(f"Token balance: {token_balance_decimal:.6f}")
            logger.info(
                f"Reference price per token: {token_price_sol:.8f} {quote_label}"
            )

            if token_balance == 0:
                logger.info("No tokens to sell.")
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message="No tokens to sell",
                )

            quote_started = monotonic_ns()
            if (
                token_info.platform == Platform.PUMP_FUN
                and pool_state is not None
                and exact_fee_schedule is not None
                and hasattr(curve_manager, "curve_state")
            ):
                exact_curve = curve_manager.curve_state(
                    pool_state, token_mint=token_info.mint
                )
                sell_quote = quote_sell(
                    tokens=TokenAmountRaw(
                        token_balance, token_info.mint, TOKEN_DECIMALS
                    ),
                    curve=exact_curve,
                    fee_rates=exact_fee_schedule.select(exact_curve),
                    slippage=slippage,
                )
                if sell_quote.minimum_output.value <= 0:
                    raise ValueError(
                        "Exact Pump sell quote produced zero minimum output"
                    )
                expected_quote_output = float(sell_quote.expected_output.to_decimal())
                min_quote_output = sell_quote.minimum_output.value
                token_price_sol = expected_quote_output / token_balance_decimal
                execution_plan = ExecutionPlan.for_sell(
                    sell_quote, logical_execution_id=logical_execution_id
                )
            else:
                # LetsBonk compatibility path; Pump uses curve output above.
                expected_quote_output = token_balance_decimal * token_price_sol
                min_quote_output = max(
                    1,
                    int(
                        (expected_quote_output * (1 - compatibility_slippage_fraction))
                        * quote_unit
                    ),
                )
            if telemetry is not None:
                telemetry.attributes["quote_generation_ms"] = (
                    monotonic_ns() - quote_started
                ) / 1_000_000
            logger.info(
                f"Selling {token_balance_decimal} tokens on {token_info.platform.value}"
            )
            logger.info(
                f"Expected {quote_label} output: {expected_quote_output:.10f} {quote_label}"
            )
            logger.info(
                f"Minimum {quote_label} output (with {compatibility_slippage_fraction * 100:.1f}% slippage): "
                f"{min_quote_output / quote_unit:.10f} {quote_label} "
                f"({min_quote_output} raw units)"
            )

            # Build sell instructions using platform-specific builder
            instruction_build_started = monotonic_ns()
            instructions = await instruction_builder.build_sell_instruction(
                token_info,
                self.wallet.pubkey,
                token_balance,  # amount_in (tokens)
                min_quote_output,  # minimum_amount_out (raw quote units)
                address_provider,
            )
            if telemetry is not None:
                telemetry.attributes["instruction_construction_ms"] = (
                    monotonic_ns() - instruction_build_started
                ) / 1_000_000
                telemetry.maximum_rent_exposure_lamports = maximum_ata_rent_lamports(
                    instructions
                )

            # Get accounts for priority fee calculation
            priority_accounts = instruction_builder.get_required_accounts_for_sell(
                token_info, self.wallet.pubkey, address_provider
            )

            # Send transaction
            priority_fee = await self.priority_fee_manager.calculate_priority_fee(
                priority_accounts
            )
            if telemetry is not None:
                _apply_priority_fee_telemetry(telemetry, self.priority_fee_manager)
            compute_unit_limit = instruction_builder.get_sell_compute_unit_limit(
                self._get_cu_override("sell", token_info.platform)
            )
            if not previously_confirmed:
                await self._assess_risk(
                    execution_plan,
                    instructions,
                    priority_fee,
                    compute_unit_limit,
                )

            if previously_confirmed:
                tx_signature = existing_signature
                success = True
                telemetry = None
            else:
                if telemetry is None:
                    raise RuntimeError("sell execution telemetry was not initialized")
                if execution_plan is not None:
                    telemetry.execution_id = execution_plan.logical_execution_id
                telemetry.apply_detection(detection_for(token_info))
                send_options = {
                    "skip_preflight": True,
                    "max_retries": self.max_retries,
                    "priority_fee": priority_fee,
                    "compute_unit_limit": compute_unit_limit,
                    "account_data_size_limit": self._get_cu_override(
                        "account_data_size", token_info.platform
                    ),
                    "telemetry": telemetry,
                }
                if submission_recorder is not None:
                    send_options["submission_callback"] = submission_recorder
                tx_signature = await self.client.build_and_send_transaction(
                    instructions, self.wallet.keypair, **send_options
                )

                success = await self.client.confirm_transaction(tx_signature)
                if not success:
                    observation = getattr(
                        self.client, "last_transaction_observation", None
                    )
                    if observation is not None:
                        telemetry.error_classification = (
                            observation.error_classification
                        )
                if not success and self.telemetry_recorder is not None:
                    self.telemetry_recorder(telemetry)

            if success:
                logger.info(f"Sell transaction confirmed: {tx_signature}")
                actual_execution = (
                    await self.client.get_execution_result(
                        execution_plan, tx_signature, self.wallet.pubkey
                    )
                    if execution_plan is not None
                    else None
                )
                if actual_execution is not None:
                    if (
                        actual_execution.token_delta_raw is None
                        or actual_execution.quote_delta_raw is None
                    ):
                        raise ValueError(
                            "Confirmed Pump sell lacks authoritative balance/event effects"
                        )
                    token_balance_decimal = float(
                        Decimal(actual_execution.token_delta_raw)
                        / Decimal(10**TOKEN_DECIMALS)
                    )
                    token_price_sol = float(
                        (
                            Decimal(actual_execution.quote_delta_raw)
                            / Decimal(quote_unit)
                        )
                        / Decimal(str(token_balance_decimal))
                    )
                    if telemetry is not None:
                        _apply_execution_result_telemetry(
                            telemetry, actual_execution, quote_mint
                        )
                if telemetry is not None and self.telemetry_recorder is not None:
                    self.telemetry_recorder(telemetry)
                return TradeResult(
                    success=True,
                    platform=token_info.platform,
                    tx_signature=tx_signature,
                    amount=token_balance_decimal,
                    price=token_price_sol,
                    quote=sell_quote,
                    execution_plan=execution_plan,
                    execution_result=actual_execution,
                    reused_existing_signature=previously_confirmed,
                )
            else:
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    tx_signature=tx_signature,
                    error_message=f"Transaction failed to confirm: {tx_signature}",
                    error_classification=(
                        self.client.last_transaction_observation.error_classification
                        if getattr(self.client, "last_transaction_observation", None)
                        else ErrorClassification.CONFIRMATION_TIMEOUT
                    ),
                )

        except Exception as e:
            failed_telemetry = locals().get("telemetry")
            if isinstance(failed_telemetry, ExecutionTelemetry):
                failed_telemetry.error_classification = (
                    e.classification
                    if hasattr(e, "classification")
                    else ErrorClassification.UNKNOWN
                )
                if self.telemetry_recorder is not None:
                    self.telemetry_recorder(failed_telemetry)
            logger.exception("Sell operation failed")
            return TradeResult(
                success=False,
                platform=token_info.platform,
                error_message=str(e),
                error_classification=(
                    e.classification
                    if hasattr(e, "classification")
                    else ErrorClassification.UNKNOWN
                ),
            )

    async def _assess_risk(
        self,
        plan: ExecutionPlan | None,
        instructions: list,
        priority_fee_micro_lamports: int,
        compute_unit_limit: int,
    ) -> None:
        if self.risk_service is None or not self.risk_service.limits.enforce:
            return
        if plan is None:
            raise ValueError("risk enforcement requires an exact execution plan")
        base_fee = await self.client.estimate_base_fee_lamports(
            instructions, self.wallet.pubkey
        )
        existing, aggregate = (
            await self.exposure_provider(plan.token_mint, plan.quote_mint)
            if self.exposure_provider is not None
            else (0, 0)
        )
        self.risk_service.assess(
            plan,
            RiskContext(
                wallet_lamports=await self.client.get_wallet_balance_lamports(
                    self.wallet.pubkey
                ),
                existing_position_exposure_raw=existing,
                aggregate_exposure_raw=aggregate,
                fee_exposure=FeeExposure(
                    Lamports(base_fee) if base_fee is not None else None,
                    priority_fee_lamports_ceiling(
                        MicroLamportsPerCU(priority_fee_micro_lamports),
                        compute_unit_limit,
                    ),
                    Lamports(maximum_ata_rent_lamports(instructions)),
                    jito_tip=Lamports(getattr(self.client, "jito_tip_lamports", 0)),
                ),
            ),
        )

    def _get_pool_address(
        self, token_info: TokenInfo, address_provider: AddressProvider
    ) -> Pubkey:
        """Get the pool/curve address for price calculations using platform-agnostic method."""
        # Try to get the address from token_info first, then derive if needed
        if token_info.platform == Platform.PUMP_FUN:
            if hasattr(token_info, "bonding_curve") and token_info.bonding_curve:
                return token_info.bonding_curve
        elif token_info.platform == Platform.LETS_BONK:
            if hasattr(token_info, "pool_state") and token_info.pool_state:
                return token_info.pool_state

        # Fallback to deriving the address using platform provider
        return address_provider.derive_pool_address(token_info.mint)

    def _get_cu_override(self, operation: str, platform: Platform) -> int | None:
        """Get compute unit override from configuration.

        Args:
            operation: "buy" or "sell"
            platform: Trading platform (unused - each config is platform-specific)

        Returns:
            CU override value if configured, None otherwise
        """
        if not self.compute_units:
            return None

        # Just check for operation override (buy/sell)
        return self.compute_units.get(operation)
