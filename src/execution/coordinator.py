"""Idempotent logical execution coordination for one RPC backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from domain.lifecycle import ExecutionState
from domain.quotes import ExecutionPlan, ExecutionResult
from execution.confirmation import TransactionObservation
from execution.errors import ErrorClassification, ExecutionError
from execution.telemetry import ExecutionTelemetry
from storage.sqlite import SQLitePositionStore


@dataclass(frozen=True, slots=True)
class SubmittedTransaction:
    signature: str
    blockhash: str
    last_valid_block_height: int


class ExecutionGateway(Protocol):
    async def submit(
        self, plan: ExecutionPlan, telemetry: ExecutionTelemetry
    ) -> SubmittedTransaction: ...

    async def observe(
        self, signature: str, last_valid_block_height: int | None
    ) -> TransactionObservation: ...

    async def inspect_result(
        self, plan: ExecutionPlan, signature: str
    ) -> ExecutionResult: ...


class ExecutionCoordinator:
    """Never resubmits an ambiguous logical trade before inspecting identity."""

    def __init__(self, gateway: ExecutionGateway, store: SQLitePositionStore):
        self.gateway = gateway
        self.store = store

    async def execute(
        self, plan: ExecutionPlan, *, position_id: str | None = None
    ) -> ExecutionResult:
        existing = self.store.create_execution(
            plan.logical_execution_id,
            position_id=position_id,
            side=plan.side.value,
        )
        if existing.side != plan.side.value or existing.position_id != position_id:
            raise ExecutionError(
                ErrorClassification.CONFIGURATION_ERROR,
                "logical execution ID was already claimed for a different trade",
            )

        if existing.signature:
            observation = await self.gateway.observe(
                existing.signature, existing.last_valid_block_height
            )
            self.store.update_execution(
                plan.logical_execution_id,
                state=observation.state,
                error_classification=observation.error_classification,
            )
            if observation.succeeded:
                return await self.gateway.inspect_result(plan, existing.signature)
            if observation.state not in {
                ExecutionState.EXPIRED,
                ExecutionState.FAILED_ON_CHAIN,
            }:
                raise ExecutionError(
                    ErrorClassification.ACCEPTED_BUT_NOT_OBSERVED,
                    "existing signature has an ambiguous non-terminal outcome",
                    retryable=True,
                )
            if observation.state == ExecutionState.FAILED_ON_CHAIN:
                raise ExecutionError(
                    ErrorClassification.ON_CHAIN_PROGRAM_FAILURE,
                    "existing transaction failed on chain",
                    retryable=False,
                )

        telemetry = ExecutionTelemetry(execution_id=plan.logical_execution_id)
        telemetry.mark("trade_requested")
        submitted = await self.gateway.submit(plan, telemetry)
        self.store.update_execution(
            plan.logical_execution_id,
            state=ExecutionState.SIGNATURE_RECEIVED,
            signature=submitted.signature,
            blockhash=submitted.blockhash,
            last_valid_block_height=submitted.last_valid_block_height,
            increment_attempt=True,
        )
        telemetry.transaction_signature = submitted.signature
        telemetry.blockhash = submitted.blockhash
        telemetry.last_valid_block_height = submitted.last_valid_block_height
        self.store.save_telemetry(telemetry, existing.submission_attempt + 1)

        observation = await self.gateway.observe(
            submitted.signature, submitted.last_valid_block_height
        )
        self.store.update_execution(
            plan.logical_execution_id,
            state=observation.state,
            error_classification=observation.error_classification,
        )
        if not observation.succeeded:
            classification = observation.error_classification or (
                ErrorClassification.ACCEPTED_BUT_NOT_OBSERVED
            )
            raise ExecutionError(
                classification,
                f"transaction ended in {observation.state.value}",
                retryable=observation.state
                in {
                    ExecutionState.TIMED_OUT,
                    ExecutionState.NOT_OBSERVED,
                    ExecutionState.EXPIRED,
                },
            )
        return await self.gateway.inspect_result(plan, submitted.signature)
