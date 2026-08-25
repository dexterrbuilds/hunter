"""Typed integer monetary values and explicit decimal conversion rules."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Self

from solders.pubkey import Pubkey

MAX_U64 = 2**64 - 1


class RoundingDirection(StrEnum):
    """Permitted conversion directions for whole units to raw integers."""

    DOWN = "down"
    UP = "up"
    NEAREST_EVEN = "nearest_even"


_ROUNDING = {
    RoundingDirection.DOWN: ROUND_FLOOR,
    RoundingDirection.UP: ROUND_CEILING,
    RoundingDirection.NEAREST_EVEN: ROUND_HALF_EVEN,
}


def validate_decimals(decimals: int) -> int:
    """Validate a mint's explicit decimal precision."""
    if isinstance(decimals, bool) or not isinstance(decimals, int):
        raise TypeError("decimals must be an integer")
    if not 0 <= decimals <= 255:
        raise ValueError("decimals must be between 0 and 255")
    return decimals


def decimal_to_raw(
    amount: Decimal | str | int,
    *,
    decimals: int,
    rounding: RoundingDirection,
) -> int:
    """Convert whole units to raw units without binary floating point.

    A rounding rule and mint decimals are mandatory at every conversion site.
    Floats are rejected so callers must make the compatibility decision explicit
    with ``Decimal(str(value))`` when migrating legacy configuration.
    """
    validate_decimals(decimals)
    if isinstance(amount, float):
        raise TypeError("binary floats are not accepted; use Decimal(str(value))")
    value = amount if isinstance(amount, Decimal) else Decimal(amount)
    if not value.is_finite() or value < 0:
        raise ValueError("amount must be finite and non-negative")
    scaled = value * (Decimal(10) ** decimals)
    raw = int(scaled.to_integral_value(rounding=_ROUNDING[rounding]))
    if raw > MAX_U64:
        raise OverflowError("raw amount exceeds u64")
    return raw


def raw_to_decimal(raw: int, *, decimals: int) -> Decimal:
    """Convert raw units to an exact decimal value."""
    _validate_raw(raw)
    validate_decimals(decimals)
    return Decimal(raw) / (Decimal(10) ** decimals)


def _validate_raw(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("raw amount must be an integer")
    if not 0 <= value <= MAX_U64:
        raise ValueError("raw amount must fit in u64")


@dataclass(frozen=True, slots=True)
class TokenAmountRaw:
    """Raw units for one explicitly identified base-token mint."""

    value: int
    mint: Pubkey
    decimals: int

    def __post_init__(self) -> None:
        _validate_raw(self.value)
        validate_decimals(self.decimals)

    @classmethod
    def from_decimal(
        cls,
        amount: Decimal | str | int,
        *,
        mint: Pubkey,
        decimals: int,
        rounding: RoundingDirection,
    ) -> Self:
        return cls(
            decimal_to_raw(amount, decimals=decimals, rounding=rounding),
            mint,
            decimals,
        )

    def to_decimal(self) -> Decimal:
        return raw_to_decimal(self.value, decimals=self.decimals)


@dataclass(frozen=True, slots=True)
class QuoteAmountRaw:
    """Raw units for one explicitly identified quote mint."""

    value: int
    mint: Pubkey
    decimals: int

    def __post_init__(self) -> None:
        _validate_raw(self.value)
        validate_decimals(self.decimals)

    @classmethod
    def from_decimal(
        cls,
        amount: Decimal | str | int,
        *,
        mint: Pubkey,
        decimals: int,
        rounding: RoundingDirection,
    ) -> Self:
        return cls(
            decimal_to_raw(amount, decimals=decimals, rounding=rounding),
            mint,
            decimals,
        )

    def to_decimal(self) -> Decimal:
        return raw_to_decimal(self.value, decimals=self.decimals)


@dataclass(frozen=True, slots=True)
class Lamports:
    """Native SOL lamports."""

    value: int

    def __post_init__(self) -> None:
        _validate_raw(self.value)


@dataclass(frozen=True, slots=True)
class BasisPoints:
    """A percentage in hundredths of one percent."""

    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise TypeError("basis points must be an integer")
        if not 0 <= self.value <= 10_000:
            raise ValueError("basis points must be between 0 and 10,000")


@dataclass(frozen=True, slots=True)
class MicroLamportsPerCU:
    """Compute-unit price used by Solana's compute budget program."""

    value: int

    def __post_init__(self) -> None:
        _validate_raw(self.value)


def floor_mul_bps(value: int, bps: BasisPoints) -> int:
    """Apply a basis-point ratio and round toward zero."""
    _validate_raw(value)
    return value * bps.value // 10_000


def ceil_div(numerator: int, denominator: int) -> int:
    """Divide non-negative integers and round upward."""
    if numerator < 0 or denominator <= 0:
        raise ValueError("ceil_div requires numerator >= 0 and denominator > 0")
    return (numerator + denominator - 1) // denominator


def minimum_after_slippage(value: int, slippage: BasisPoints) -> int:
    """Return a conservative output floor, rounded downward."""
    _validate_raw(value)
    return value * (10_000 - slippage.value) // 10_000


def maximum_after_slippage(value: int, slippage: BasisPoints) -> int:
    """Return a conservative input cap, rounded upward."""
    _validate_raw(value)
    return ceil_div(value * (10_000 + slippage.value), 10_000)


def priority_fee_lamports_ceiling(
    price: MicroLamportsPerCU, compute_unit_limit: int
) -> Lamports:
    """Maximum priority fee, with protocol-safe upward rounding."""
    if isinstance(compute_unit_limit, bool) or not isinstance(compute_unit_limit, int):
        raise TypeError("compute unit limit must be an integer")
    if compute_unit_limit < 0:
        raise ValueError("compute unit limit must be non-negative")
    return Lamports(ceil_div(price.value * compute_unit_limit, 1_000_000))
