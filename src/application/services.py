"""Buy/sell orchestration around quotes, risk, execution, and positions."""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol

from application.models import BuyRequest, SellRequest
from application.positions import PositionService
from application.risk import RiskContext, RiskService
from domain.lifecycle import PositionStatus
from domain.quotes import BuyQuote, ExecutionPlan, ExecutionResult, SellQuote
from execution.coordinator import ExecutionCoordinator
from execution.errors import ExecutionError


class BuyQuoteProvider(Protocol):
    async def quote_buy(self, request: BuyRequest) -> BuyQuote: ...


class SellQuoteProvider(Protocol):
    async def quote_sell(self, request: SellRequest) -> SellQuote: ...


RiskContextFactory = Callable[[ExecutionPlan], Awaitable[RiskContext]]


class BuyService:
    """Quote and execute a buy without listener/interface dependencies."""

    def __init__(
        self,
        quote_provider: BuyQuoteProvider,
        coordinator: ExecutionCoordinator,
        risk_service: RiskService,
        risk_context_factory: RiskContextFactory,
        position_service: PositionService,
    ):
        self.quote_provider = quote_provider
        self.coordinator = coordinator
        self.risk_service = risk_service
        self.risk_context_factory = risk_context_factory
        self.position_service = position_service

    async def buy(self, request: BuyRequest) -> ExecutionResult:
        quote = await self.quote_provider.quote_buy(request)
        plan = request.plan or ExecutionPlan.for_buy(
            quote, logical_execution_id=request.logical_execution_id
        )
        self.risk_service.assess(plan, await self.risk_context_factory(plan))
        result = await self.coordinator.execute(plan)
        if result.success:
            self.risk_service.record_trade()
            self.position_service.open_from_execution(
                result,
                token_mint=request.token_mint,
                quote_mint=request.quote_mint,
                token_decimals=request.token_decimals,
                quote_decimals=request.spend.decimals,
            )
        return result


class SellService:
    """Durable sell lifecycle with idempotent ambiguous confirmation handling."""

    def __init__(
        self,
        quote_provider: SellQuoteProvider,
        coordinator: ExecutionCoordinator,
        risk_service: RiskService,
        risk_context_factory: RiskContextFactory,
        position_service: PositionService,
    ):
        self.quote_provider = quote_provider
        self.coordinator = coordinator
        self.risk_service = risk_service
        self.risk_context_factory = risk_context_factory
        self.position_service = position_service

    async def sell(self, request: SellRequest) -> ExecutionResult:
        position = self.position_service.get_position(request.position_id)
        status = position.accounting.status
        if status == PositionStatus.OPEN:
            self.position_service.store.transition_position(
                request.position_id,
                PositionStatus.EXIT_REQUESTED,
                "sell requested",
            )
        elif status not in {
            PositionStatus.EXIT_REQUESTED,
            PositionStatus.SELL_FAILED_RETRYABLE,
            PositionStatus.SELL_SUBMITTED,
        }:
            raise ValueError(f"position cannot be sold from state {status.value}")

        quote = await self.quote_provider.quote_sell(request)
        plan = request.plan or ExecutionPlan.for_sell(
            quote, logical_execution_id=request.logical_execution_id
        )
        self.risk_service.assess(plan, await self.risk_context_factory(plan))
        current = self.position_service.get_position(request.position_id)
        if current.accounting.status == PositionStatus.SELL_FAILED_RETRYABLE:
            self.position_service.store.transition_position(
                request.position_id,
                PositionStatus.EXIT_REQUESTED,
                "safe retry after signature inspection",
            )
        self.position_service.store.transition_position(
            request.position_id,
            PositionStatus.SELL_SUBMITTED,
            f"logical execution {plan.logical_execution_id}",
        )
        try:
            result = await self.coordinator.execute(
                plan, position_id=request.position_id
            )
        except ExecutionError as error:
            target = (
                PositionStatus.SELL_FAILED_RETRYABLE
                if error.retryable
                else PositionStatus.SELL_FAILED_PERMANENT
            )
            self.position_service.store.transition_position(
                request.position_id, target, error.classification.value
            )
            raise

        self.position_service.store.transition_position(
            request.position_id,
            PositionStatus.SELL_CONFIRMED,
            result.signature,
        )
        self.position_service.apply_sell_execution(request.position_id, result)
        self.risk_service.record_trade()
        return result
