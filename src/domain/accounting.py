"""Execution-based position accounting with average raw-unit cost basis."""

from __future__ import annotations

from dataclasses import dataclass, field

from solders.pubkey import Pubkey

from domain.lifecycle import PositionStatus


@dataclass(frozen=True, slots=True)
class BuyFill:
    signature: str
    token_quantity_raw: int
    quote_cost_raw: int
    network_fee_lamports: int | None
    priority_fee_lamports: int | None
    other_cost_lamports: int | None = 0


@dataclass(frozen=True, slots=True)
class SellFill:
    signature: str
    token_quantity_raw: int
    quote_proceeds_raw: int
    network_fee_lamports: int | None
    priority_fee_lamports: int | None
    other_cost_lamports: int | None = 0


@dataclass(frozen=True, slots=True)
class RealizedPnl:
    """Raw quote PnL; net values are None when costs cannot be converted."""

    gross_raw: int
    net_raw: int | None
    return_bps: int | None
    allocated_cost_basis_raw: int
    remaining_cost_basis_raw: int
    remaining_quantity_raw: int
    unknown_costs: tuple[str, ...]


@dataclass(slots=True)
class PositionAccounting:
    """Persistent-friendly aggregate for buys and partial exits."""

    position_id: str
    token_mint: Pubkey
    quote_mint: Pubkey
    token_decimals: int
    quote_decimals: int
    status: PositionStatus = PositionStatus.OPEN
    acquired_quantity_raw: int = 0
    sold_quantity_raw: int = 0
    quote_cost_raw: int = 0
    quote_proceeds_raw: int = 0
    remaining_cost_basis_raw: int = 0
    realized_gross_pnl_raw: int = 0
    realized_net_pnl_raw: int | None = 0
    entry_network_fee_lamports: int | None = 0
    exit_network_fee_lamports: int | None = 0
    entry_priority_fee_lamports: int | None = 0
    exit_priority_fee_lamports: int | None = 0
    other_entry_cost_lamports: int | None = 0
    other_exit_cost_lamports: int | None = 0
    remaining_entry_cost_lamports: int | None = 0
    unknown_costs: set[str] = field(default_factory=set)

    @property
    def remaining_quantity_raw(self) -> int:
        return self.acquired_quantity_raw - self.sold_quantity_raw

    def record_buy(self, fill: BuyFill) -> None:
        """Add an authoritative acquisition fill."""
        _validate_fill(fill.token_quantity_raw, fill.quote_cost_raw)
        if fill.token_quantity_raw == 0:
            raise ValueError("buy fill quantity must be positive")
        self.acquired_quantity_raw += fill.token_quantity_raw
        self.quote_cost_raw += fill.quote_cost_raw
        self.remaining_cost_basis_raw += fill.quote_cost_raw
        self.entry_network_fee_lamports = _add_known(
            self.entry_network_fee_lamports,
            fill.network_fee_lamports,
            self.unknown_costs,
            "entry_network_fee",
        )
        self.entry_priority_fee_lamports = _add_known(
            self.entry_priority_fee_lamports,
            fill.priority_fee_lamports,
            self.unknown_costs,
            "entry_priority_fee",
        )
        self.other_entry_cost_lamports = _add_known(
            self.other_entry_cost_lamports,
            fill.other_cost_lamports,
            self.unknown_costs,
            "other_entry_cost",
        )
        combined = _known_sum(fill.network_fee_lamports, fill.other_cost_lamports)
        self.remaining_entry_cost_lamports = _add_known(
            self.remaining_entry_cost_lamports,
            combined,
            self.unknown_costs,
            "entry_cost_allocation",
        )

    def record_sell(self, fill: SellFill, *, quote_is_native_sol: bool) -> RealizedPnl:
        """Apply an authoritative partial/full sale using average cost basis."""
        _validate_fill(fill.token_quantity_raw, fill.quote_proceeds_raw)
        if fill.token_quantity_raw <= 0:
            raise ValueError("sell fill quantity must be positive")
        before_quantity = self.remaining_quantity_raw
        if fill.token_quantity_raw > before_quantity:
            raise ValueError("sell quantity exceeds remaining inventory")

        if fill.token_quantity_raw == before_quantity:
            allocated_basis = self.remaining_cost_basis_raw
        else:
            allocated_basis = (
                self.remaining_cost_basis_raw
                * fill.token_quantity_raw
                // before_quantity
            )
        allocated_entry_cost = self._allocate_entry_cost(
            fill.token_quantity_raw, before_quantity
        )

        gross = fill.quote_proceeds_raw - allocated_basis
        self.sold_quantity_raw += fill.token_quantity_raw
        self.quote_proceeds_raw += fill.quote_proceeds_raw
        self.remaining_cost_basis_raw -= allocated_basis
        self.realized_gross_pnl_raw += gross
        self.exit_network_fee_lamports = _add_known(
            self.exit_network_fee_lamports,
            fill.network_fee_lamports,
            self.unknown_costs,
            "exit_network_fee",
        )
        self.exit_priority_fee_lamports = _add_known(
            self.exit_priority_fee_lamports,
            fill.priority_fee_lamports,
            self.unknown_costs,
            "exit_priority_fee",
        )
        self.other_exit_cost_lamports = _add_known(
            self.other_exit_cost_lamports,
            fill.other_cost_lamports,
            self.unknown_costs,
            "other_exit_cost",
        )

        net: int | None = None
        sale_unknowns = set(self.unknown_costs)
        if not quote_is_native_sol:
            native_costs = (
                allocated_entry_cost,
                fill.network_fee_lamports,
                fill.other_cost_lamports,
            )
            if any(value is None for value in native_costs):
                sale_unknowns.add("unknown_native_execution_cost")
            elif any(value != 0 for value in native_costs):
                sale_unknowns.add("native_fees_require_quote_conversion")
            else:
                net = gross
        elif (
            allocated_entry_cost is not None
            and fill.network_fee_lamports is not None
            and fill.other_cost_lamports is not None
        ):
            net = (
                gross
                - allocated_entry_cost
                - fill.network_fee_lamports
                - fill.other_cost_lamports
            )

        if self.realized_net_pnl_raw is not None and net is not None:
            self.realized_net_pnl_raw += net
        else:
            self.realized_net_pnl_raw = None

        return_bps = None
        total_cost_for_return = allocated_basis
        if net is not None and allocated_entry_cost is not None:
            total_cost_for_return += allocated_entry_cost
            if total_cost_for_return > 0:
                return_bps = net * 10_000 // total_cost_for_return

        return RealizedPnl(
            gross_raw=gross,
            net_raw=net,
            return_bps=return_bps,
            allocated_cost_basis_raw=allocated_basis,
            remaining_cost_basis_raw=self.remaining_cost_basis_raw,
            remaining_quantity_raw=self.remaining_quantity_raw,
            unknown_costs=tuple(sorted(sale_unknowns)),
        )

    def _allocate_entry_cost(self, quantity: int, before_quantity: int) -> int | None:
        if self.remaining_entry_cost_lamports is None:
            return None
        if quantity == before_quantity:
            allocated = self.remaining_entry_cost_lamports
        else:
            allocated = self.remaining_entry_cost_lamports * quantity // before_quantity
        self.remaining_entry_cost_lamports -= allocated
        return allocated


def _validate_fill(quantity: int, quote: int) -> None:
    if quantity < 0 or quote < 0:
        raise ValueError("fill amounts must be non-negative")


def _known_sum(first: int | None, second: int | None) -> int | None:
    if first is None or second is None:
        return None
    return first + second


def _add_known(
    current: int | None,
    incoming: int | None,
    unknowns: set[str],
    label: str,
) -> int | None:
    if current is None or incoming is None:
        unknowns.add(label)
        return None
    return current + incoming
