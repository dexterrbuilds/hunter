"""Deterministic Pump bonding-curve quotes and execution models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from solders.pubkey import Pubkey

from domain.amounts import (
    BasisPoints,
    QuoteAmountRaw,
    TokenAmountRaw,
    ceil_div,
    maximum_after_slippage,
    minimum_after_slippage,
)

BPS_DENOMINATOR = 10_000
PUMP_STANDARD_SUPPLY_RAW = 1_000_000_000_000_000


@dataclass(frozen=True, slots=True)
class CurveState:
    """The complete integer reserve state used for one quote."""

    token_mint: Pubkey
    quote_mint: Pubkey
    token_decimals: int
    quote_decimals: int
    virtual_token_reserves: int
    virtual_quote_reserves: int
    real_token_reserves: int
    token_total_supply: int
    creator_present: bool
    is_mayhem_mode: bool = False
    slot: int | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        for name in (
            "virtual_token_reserves",
            "virtual_quote_reserves",
            "real_token_reserves",
            "token_total_supply",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.virtual_token_reserves == 0:
            raise ValueError("virtual token reserves cannot be zero")
        if self.virtual_quote_reserves == 0:
            raise ValueError("virtual quote reserves cannot be zero")


@dataclass(frozen=True, slots=True)
class FeeRates:
    """Authoritative fee rates selected for a curve state."""

    protocol_fee_bps: BasisPoints
    creator_fee_bps: BasisPoints
    lp_fee_bps: BasisPoints = field(default_factory=lambda: BasisPoints(0))
    source: str = "explicit"

    @property
    def total_trade_fee_bps(self) -> int:
        return self.protocol_fee_bps.value + self.creator_fee_bps.value


@dataclass(frozen=True, slots=True)
class FeeTier:
    """Pump fee tier keyed by an integer market-cap threshold."""

    market_cap_raw_threshold: int
    rates: FeeRates

    def __post_init__(self) -> None:
        if self.market_cap_raw_threshold < 0:
            raise ValueError("fee tier threshold must be non-negative")


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Decoded current Pump fee configuration."""

    tiers: tuple[FeeTier, ...]
    fallback: FeeRates | None = None
    source_slot: int | None = None

    def select(self, curve: CurveState) -> FeeRates:
        """Select transaction fee rates using Pump's trade-fee supply rule.

        Pump's SDK uses the canonical one-billion-token supply for established
        non-mayhem curves when pricing the fee charged for an exact token
        amount. Mayhem curves use their current mint supply.
        """
        supply = (
            curve.token_total_supply
            if curve.is_mayhem_mode
            else PUMP_STANDARD_SUPPLY_RAW
        )
        return self._select_for_supply(curve, supply)

    def select_for_buy_budget(self, curve: CurveState) -> FeeRates:
        """Select rates used when deriving token output from a quote budget.

        The official ``getBuyTokenAmountFromSolAmount`` path passes the actual
        mint supply into fee-tier selection, including for non-mayhem curves.
        This deliberately differs from :meth:`select`, which mirrors
        ``getFee`` for an exact token amount.
        """
        return self._select_for_supply(curve, curve.token_total_supply)

    def _select_for_supply(self, curve: CurveState, supply: int) -> FeeRates:
        if not self.tiers:
            if self.fallback is None:
                raise ValueError("fee schedule contains no authoritative rates")
            return self.fallback
        market_cap = bonding_curve_market_cap(
            mint_supply=supply,
            virtual_quote_reserves=curve.virtual_quote_reserves,
            virtual_token_reserves=curve.virtual_token_reserves,
        )
        selected = self.tiers[0]
        for tier in reversed(self.tiers):
            if market_cap >= tier.market_cap_raw_threshold:
                selected = tier
                break
        rates = selected.rates
        if not curve.creator_present:
            rates = FeeRates(
                protocol_fee_bps=rates.protocol_fee_bps,
                creator_fee_bps=BasisPoints(0),
                lp_fee_bps=rates.lp_fee_bps,
                source=rates.source,
            )
        return rates


def bonding_curve_market_cap(
    *, mint_supply: int, virtual_quote_reserves: int, virtual_token_reserves: int
) -> int:
    """Pump SDK market-cap formula, rounded downward."""
    if virtual_token_reserves <= 0:
        raise ValueError("virtual token reserves must be positive")
    if mint_supply < 0 or virtual_quote_reserves < 0:
        raise ValueError("supply and quote reserves must be non-negative")
    return virtual_quote_reserves * mint_supply // virtual_token_reserves


def fee_amount(amount: int, fee_bps: BasisPoints) -> int:
    """Pump protocol fee component, rounded upward independently."""
    if amount < 0:
        raise ValueError("fee input must be non-negative")
    return ceil_div(amount * fee_bps.value, BPS_DENOMINATOR)


def gross_buy_tokens(
    quote_input: int, virtual_token_reserves: int, virtual_quote_reserves: int
) -> int:
    """Constant-product token output after fees have been removed."""
    if quote_input < 0 or virtual_token_reserves <= 0 or virtual_quote_reserves <= 0:
        raise ValueError("buy reserves must be positive and input non-negative")
    return (
        quote_input * virtual_token_reserves // (virtual_quote_reserves + quote_input)
    )


def gross_sell_quote(
    token_input: int, virtual_token_reserves: int, virtual_quote_reserves: int
) -> int:
    """Constant-product quote output before sell fees."""
    if token_input < 0 or virtual_token_reserves <= 0 or virtual_quote_reserves <= 0:
        raise ValueError("sell reserves must be positive and input non-negative")
    return (
        token_input * virtual_quote_reserves // (virtual_token_reserves + token_input)
    )


@dataclass(frozen=True, slots=True)
class BuyQuote:
    """A fee-aware, reserve-pinned buy estimate."""

    input: QuoteAmountRaw
    expected_output: TokenAmountRaw
    minimum_output: TokenAmountRaw
    maximum_input: QuoteAmountRaw
    slippage: BasisPoints
    protocol_fee_raw: int
    creator_fee_raw: int
    curve_input_raw: int
    reserve_state: CurveState
    fee_rates: FeeRates
    trade_fee_rates: FeeRates


@dataclass(frozen=True, slots=True)
class SellQuote:
    """A fee-aware, reserve-pinned sell estimate."""

    input: TokenAmountRaw
    gross_output: QuoteAmountRaw
    expected_output: QuoteAmountRaw
    minimum_output: QuoteAmountRaw
    slippage: BasisPoints
    protocol_fee_raw: int
    creator_fee_raw: int
    reserve_state: CurveState
    fee_rates: FeeRates


def quote_buy(
    *,
    spend: QuoteAmountRaw,
    curve: CurveState,
    fee_rates: FeeRates,
    trade_fee_rates: FeeRates | None = None,
    slippage: BasisPoints,
) -> BuyQuote:
    """Quote a Pump buy using the official integer SDK ordering."""
    _validate_quote_identity(spend, curve)
    trade_rates = trade_fee_rates or fee_rates
    if spend.value == 0:
        zero = TokenAmountRaw(0, curve.token_mint, curve.token_decimals)
        return BuyQuote(
            spend,
            zero,
            zero,
            spend,
            slippage,
            0,
            0,
            0,
            curve,
            fee_rates,
            trade_rates,
        )
    total_fee_bps = fee_rates.total_trade_fee_bps
    curve_input = (
        (spend.value - 1) * BPS_DENOMINATOR // (BPS_DENOMINATOR + total_fee_bps)
    )
    expected_value = min(
        gross_buy_tokens(
            curve_input,
            curve.virtual_token_reserves,
            curve.virtual_quote_reserves,
        ),
        curve.real_token_reserves,
    )
    minimum_value = minimum_after_slippage(expected_value, slippage)
    expected = TokenAmountRaw(expected_value, curve.token_mint, curve.token_decimals)
    minimum = TokenAmountRaw(minimum_value, curve.token_mint, curve.token_decimals)

    # The v2 instruction buys the encoded base amount. Quote the expected cost
    # for that exact amount, including independently rounded fee components.
    if expected_value == 0:
        curve_cost = 0
    elif expected_value >= curve.virtual_token_reserves:
        raise ValueError("buy output exhausts virtual token reserves")
    else:
        curve_cost = (
            expected_value
            * curve.virtual_quote_reserves
            // (curve.virtual_token_reserves - expected_value)
        ) + 1
    protocol_fee = fee_amount(curve_cost, trade_rates.protocol_fee_bps)
    creator_fee = fee_amount(curve_cost, trade_rates.creator_fee_bps)
    return BuyQuote(
        input=spend,
        expected_output=expected,
        minimum_output=minimum,
        maximum_input=QuoteAmountRaw(
            maximum_after_slippage(spend.value, slippage),
            spend.mint,
            spend.decimals,
        ),
        slippage=slippage,
        protocol_fee_raw=protocol_fee,
        creator_fee_raw=creator_fee,
        curve_input_raw=curve_cost,
        reserve_state=curve,
        fee_rates=fee_rates,
        trade_fee_rates=trade_rates,
    )


def quote_sell(
    *,
    tokens: TokenAmountRaw,
    curve: CurveState,
    fee_rates: FeeRates,
    slippage: BasisPoints,
) -> SellQuote:
    """Quote a Pump sell after price impact and all known protocol fees."""
    _validate_token_identity(tokens, curve)
    gross_value = gross_sell_quote(
        tokens.value,
        curve.virtual_token_reserves,
        curve.virtual_quote_reserves,
    )
    protocol_fee = fee_amount(gross_value, fee_rates.protocol_fee_bps)
    creator_fee = fee_amount(gross_value, fee_rates.creator_fee_bps)
    expected_value = max(0, gross_value - protocol_fee - creator_fee)
    minimum_value = minimum_after_slippage(expected_value, slippage)
    amount_args = (curve.quote_mint, curve.quote_decimals)
    return SellQuote(
        input=tokens,
        gross_output=QuoteAmountRaw(gross_value, *amount_args),
        expected_output=QuoteAmountRaw(expected_value, *amount_args),
        minimum_output=QuoteAmountRaw(minimum_value, *amount_args),
        slippage=slippage,
        protocol_fee_raw=protocol_fee,
        creator_fee_raw=creator_fee,
        reserve_state=curve,
        fee_rates=fee_rates,
    )


class ExecutionSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """Immutable input to transaction construction and submission."""

    logical_execution_id: str
    side: ExecutionSide
    token_mint: Pubkey
    quote_mint: Pubkey
    input_raw: int
    expected_output_raw: int
    limit_output_raw: int
    quote: BuyQuote | SellQuote
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    intent_source: str | None = None
    execution_urgency: str | None = None
    intent_received_at: datetime | None = None
    intent_received_mono_ns: int | None = None
    quote_ready_at: datetime | None = None
    quote_ready_mono_ns: int | None = None
    risk_started_at: datetime | None = None
    risk_started_mono_ns: int | None = None
    risk_approved_at: datetime | None = None
    risk_approved_mono_ns: int | None = None

    @classmethod
    def for_buy(
        cls, quote: BuyQuote, logical_execution_id: str | None = None
    ) -> "ExecutionPlan":
        return cls(
            logical_execution_id or str(uuid4()),
            ExecutionSide.BUY,
            quote.reserve_state.token_mint,
            quote.reserve_state.quote_mint,
            quote.input.value,
            quote.expected_output.value,
            quote.minimum_output.value,
            quote,
        )

    @classmethod
    def for_sell(
        cls, quote: SellQuote, logical_execution_id: str | None = None
    ) -> "ExecutionPlan":
        return cls(
            logical_execution_id or str(uuid4()),
            ExecutionSide.SELL,
            quote.reserve_state.token_mint,
            quote.reserve_state.quote_mint,
            quote.input.value,
            quote.expected_output.value,
            quote.minimum_output.value,
            quote,
        )


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Authoritative observed effects of one logical execution."""

    logical_execution_id: str
    side: ExecutionSide
    signature: str
    success: bool
    token_delta_raw: int | None
    quote_delta_raw: int | None
    network_fee_lamports: int | None
    priority_fee_lamports: int | None
    protocol_fee_raw: int | None
    creator_fee_raw: int | None
    rent_lamports: int | None
    slot: int | None
    delivery_tip_lamports: int = 0
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    unknown_costs: tuple[str, ...] = ()
    error: str | None = None


def _validate_quote_identity(amount: QuoteAmountRaw, curve: CurveState) -> None:
    if amount.mint != curve.quote_mint or amount.decimals != curve.quote_decimals:
        raise ValueError("quote amount mint/decimals do not match curve state")


def _validate_token_identity(amount: TokenAmountRaw, curve: CurveState) -> None:
    if amount.mint != curve.token_mint or amount.decimals != curve.token_decimals:
        raise ValueError("token amount mint/decimals do not match curve state")
