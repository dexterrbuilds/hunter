"""Pump.fun launch and operator wallet-fleet orchestration."""

# Explicit orchestration signatures keep economic inputs reviewable.
# ruff: noqa: C901, PLR0912, PLR0913, TC001, TRY003, TRY301

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Protocol

from solders.instruction import Instruction
from solders.pubkey import Pubkey

from core.pubkeys import is_sol_paired
from domain.launch import (
    ComponentState,
    FleetExecutionPolicy,
    FleetWallet,
    LaunchComponent,
    LaunchExecutionPlan,
    LaunchState,
    TokenLaunchRequest,
)
from execution.bundles import (
    JITO_MAX_BUNDLE_TRANSACTIONS,
    BundleObservationState,
    BundleSubmissionResult,
    JitoBundleSubmitter,
)
from execution.errors import ErrorClassification, ExecutionError
from execution.ports import (
    BlockhashContext,
    BlockhashProvider,
    ExecutionContext,
    SignedTransaction,
    SubmissionResult,
)
from platforms.pumpfun.launch_builder import PumpFunLaunchInstructionBuilder


class LaunchStore(Protocol):
    def create_launch_plan(self, **values: object) -> bool: ...

    def get_launch_plan(self, plan_id: str) -> dict | None: ...

    def update_launch_plan(self, plan_id: str, **values: object) -> None: ...

    def save_launch_component(self, **values: object) -> None: ...

    def list_launch_components(self, plan_id: str) -> list[dict]: ...

    def update_launch_component_state(
        self, plan_id: str, component_id: str, state: str
    ) -> None: ...


class FleetSignerRegistry(Protocol):
    """Resolve and use signer handles without exposing key bytes."""

    def public_key(self, signer_id: str) -> Pubkey: ...

    async def sign_instructions(
        self,
        *,
        instructions: list[Instruction],
        fee_payer_id: str,
        required_signer_ids: tuple[str, ...],
        blockhash: BlockhashContext,
    ) -> SignedTransaction: ...


class FleetBalanceReader(Protocol):
    async def native_balance_lamports(self, wallet: Pubkey) -> int: ...


BuyInstructionFactory = Callable[
    [TokenLaunchRequest, LaunchComponent, Pubkey], Awaitable[list[Instruction]]
]
ComponentSubmitter = Callable[
    [SignedTransaction, ExecutionContext], Awaitable[SubmissionResult]
]


@dataclass(frozen=True, slots=True)
class LaunchRiskLimits:
    """Mandatory hard bounds for a multi-wallet economic launch."""

    enforce: bool = False
    maximum_creator_buy_raw: int = 0
    maximum_additional_wallet_buy_raw: int = 0
    maximum_aggregate_launch_spend_raw: int = 0
    maximum_wallet_count: int = 1
    maximum_total_priority_fees_lamports: int = 0
    maximum_bundle_tip_lamports: int = 0
    maximum_combined_transaction_cost_lamports: int = 0
    minimum_wallet_reserve_lamports: int = 0
    maximum_simultaneous_launch_exposure_raw: int = 0


@dataclass(frozen=True, slots=True)
class LaunchCostEstimate:
    priority_fees_lamports: int
    bundle_tip_lamports: int
    base_fees_lamports: int
    rent_lamports: int

    @property
    def combined_lamports(self) -> int:
        return (
            self.priority_fees_lamports
            + self.bundle_tip_lamports
            + self.base_fees_lamports
            + self.rent_lamports
        )


@dataclass(frozen=True, slots=True)
class PreparedLaunchComponent:
    component: LaunchComponent
    transaction: SignedTransaction
    context: ExecutionContext


@dataclass(frozen=True, slots=True)
class LaunchSubmission:
    plan: LaunchExecutionPlan
    state: LaunchState
    component_signatures: tuple[str, ...]
    bundle_result: BundleSubmissionResult | None = None
    submission_results: tuple[SubmissionResult, ...] = ()


class LaunchRiskService:
    """Fail closed before any launch component is signed."""

    def __init__(self, limits: LaunchRiskLimits) -> None:
        self.limits = limits

    def assess(
        self,
        request: TokenLaunchRequest,
        plan: LaunchExecutionPlan,
        *,
        wallet_balances: dict[str, int],
        estimated_cost: LaunchCostEstimate,
        bundle_capacity: int | None,
        active_launch_exposure_raw: int,
    ) -> None:
        limits = self.limits
        if not limits.enforce:
            raise ExecutionError(
                ErrorClassification.RISK_LIMIT_EXCEEDED,
                "wallet-fleet launches require launch risk enforcement",
            )
        if not is_sol_paired(request.creator_buy.mint):
            raise ExecutionError(
                ErrorClassification.UNSUPPORTED_QUOTE_TOKEN,
                "token launch currently supports SOL-quoted create_v2 only",
            )
        wallet_count = 1 + len(request.additional_wallet_buys)
        if wallet_count > limits.maximum_wallet_count:
            raise self._reject("wallet count exceeds launch maximum")
        if request.creator_buy.value > limits.maximum_creator_buy_raw:
            raise self._reject("creator buy exceeds launch maximum")
        if any(
            item.quote_amount.value > limits.maximum_additional_wallet_buy_raw
            for item in request.additional_wallet_buys
        ):
            raise self._reject("additional wallet buy exceeds launch maximum")
        aggregate = request.creator_buy.value + sum(
            item.quote_amount.value for item in request.additional_wallet_buys
        )
        if aggregate > limits.maximum_aggregate_launch_spend_raw:
            raise self._reject("aggregate launch spend exceeds maximum")
        if (
            active_launch_exposure_raw + aggregate
            > limits.maximum_simultaneous_launch_exposure_raw
        ):
            raise self._reject("simultaneous launch exposure exceeds maximum")
        if (
            estimated_cost.priority_fees_lamports
            > limits.maximum_total_priority_fees_lamports
        ):
            raise self._reject("launch priority fees exceed maximum")
        if estimated_cost.bundle_tip_lamports > limits.maximum_bundle_tip_lamports:
            raise self._reject("bundle tip exceeds maximum")
        if (
            estimated_cost.combined_lamports
            > limits.maximum_combined_transaction_cost_lamports
        ):
            raise self._reject("combined launch transaction costs exceed maximum")
        if plan.execution_policy == FleetExecutionPolicy.BUNDLE:
            if bundle_capacity is None or len(plan.components) > bundle_capacity:
                raise self._reject("launch plan exceeds provider bundle capacity")
        spends = {request.creator_wallet_id: request.creator_buy.value}
        spends.update(
            {
                item.wallet_id: item.quote_amount.value
                for item in request.additional_wallet_buys
            }
        )
        for wallet_id, spend in spends.items():
            required = spend + limits.minimum_wallet_reserve_lamports
            if wallet_id == request.creator_wallet_id:
                required += estimated_cost.rent_lamports
            if wallet_balances.get(wallet_id, -1) < required:
                raise ExecutionError(
                    ErrorClassification.INSUFFICIENT_BALANCE,
                    f"wallet {wallet_id} would fall below launch reserve",
                )

    @staticmethod
    def _reject(message: str) -> ExecutionError:
        return ExecutionError(ErrorClassification.RISK_LIMIT_EXCEEDED, message)


class PumpFunLaunchComponentPreparer:
    """Prepare create_v2 and buy_v2 components using one fresh blockhash."""

    def __init__(
        self,
        launch_builder: PumpFunLaunchInstructionBuilder,
        buy_instruction_factory: BuyInstructionFactory,
        signer_registry: FleetSignerRegistry,
        wallets: dict[str, FleetWallet],
    ) -> None:
        self.launch_builder = launch_builder
        self.buy_instruction_factory = buy_instruction_factory
        self.signers = signer_registry
        self.wallets = wallets

    async def prepare(
        self,
        request: TokenLaunchRequest,
        component: LaunchComponent,
        blockhash: BlockhashContext,
        *,
        execution_variant: str,
        compute_unit_limit: int | None,
        compute_unit_price_micro_lamports: int | None,
        priority_fee_lamports: int,
        jito_tip_lamports: int,
    ) -> PreparedLaunchComponent:
        wallet = self.wallets[component.wallet_id]
        wallet_pubkey = wallet.signer.expected_public_key
        if component.action == "create":
            instructions = self.launch_builder.build_create_transaction_instructions(
                mint=request.mint,
                user=wallet_pubkey,
                creator=wallet_pubkey,
                name=request.name,
                symbol=request.symbol,
                uri=request.uri,
                is_mayhem_mode=request.is_mayhem_mode,
                is_cashback_enabled=request.is_cashback_enabled,
            )
        else:
            instructions = await self.buy_instruction_factory(
                request, component, wallet_pubkey
            )
        transaction = await self.signers.sign_instructions(
            instructions=instructions,
            fee_payer_id=component.wallet_id,
            required_signer_ids=component.required_signer_ids,
            blockhash=blockhash,
        )
        context = ExecutionContext(
            logical_trade_id=request.launch_id,
            execution_id=component.logical_execution_id,
            execution_variant=execution_variant,
            blockhash=blockhash,
            signature=transaction.signature,
            compute_unit_limit=compute_unit_limit,
            compute_unit_price_micro_lamports=compute_unit_price_micro_lamports,
            priority_fee_lamports=priority_fee_lamports,
            jito_tip_lamports=jito_tip_lamports,
            metadata={
                "intent_source": "token_launch",
                "launch_plan_id": f"launch:{request.launch_id}",
                "launch_component": component.component_id,
                "wallet_id": component.wallet_id,
            },
        )
        return PreparedLaunchComponent(component, transaction, context)


class TokenLaunchService:
    """Durable launch planner; transport acknowledgements never imply landing."""

    def __init__(
        self,
        *,
        wallets: tuple[FleetWallet, ...],
        signer_registry: FleetSignerRegistry,
        balance_reader: FleetBalanceReader,
        blockhash_provider: BlockhashProvider,
        preparer: PumpFunLaunchComponentPreparer,
        risk_service: LaunchRiskService,
        store: LaunchStore,
        component_submitter: ComponentSubmitter,
        bundle_submitter: JitoBundleSubmitter | None = None,
        maximum_prepare_concurrency: int = 4,
        maximum_blockhash_age_ms: int = 30_000,
    ) -> None:
        self.wallets = {wallet.wallet_id: wallet for wallet in wallets}
        self.signers = signer_registry
        self.balance_reader = balance_reader
        self.blockhash_provider = blockhash_provider
        self.preparer = preparer
        self.risk = risk_service
        self.store = store
        self.submit_component = component_submitter
        self.bundle_submitter = bundle_submitter
        self.maximum_prepare_concurrency = maximum_prepare_concurrency
        self.maximum_blockhash_age_ms = maximum_blockhash_age_ms

    async def execute(
        self,
        request: TokenLaunchRequest,
        *,
        estimated_cost: LaunchCostEstimate,
        active_launch_exposure_raw: int = 0,
        execution_variant: str = "standard",
        compute_unit_limit: int | None = None,
        compute_unit_price_micro_lamports: int | None = None,
    ) -> LaunchSubmission:
        plan = LaunchExecutionPlan.from_request(request)
        claimed = await asyncio.to_thread(
            self.store.create_launch_plan,
            plan_id=plan.plan_id,
            launch_id=plan.launch_id,
            mint=str(plan.mint),
            state=LaunchState.DRAFT.value,
            execution_policy=plan.execution_policy.value,
            exit_policy=asdict(plan.exit_policy),
        )
        if not claimed:
            existing = await asyncio.to_thread(self.store.get_launch_plan, plan.plan_id)
            raise ExecutionError(
                ErrorClassification.ACCEPTED_BUT_NOT_OBSERVED,
                f"launch ID already exists in state {existing['state'] if existing else 'unknown'}; inspect before retry",
            )
        try:
            balances = await self._preflight_signers_and_balances(request)
            capacity = (
                JITO_MAX_BUNDLE_TRANSACTIONS
                if self.bundle_submitter is not None
                else None
            )
            self.risk.assess(
                request,
                plan,
                wallet_balances=balances,
                estimated_cost=estimated_cost,
                bundle_capacity=capacity,
                active_launch_exposure_raw=active_launch_exposure_raw,
            )
            await asyncio.to_thread(
                self.store.update_launch_plan,
                plan.plan_id,
                state=LaunchState.VALIDATED.value,
            )
            blockhash = await self.blockhash_provider.get_blockhash()
            if not blockhash.is_acceptable_age(self.maximum_blockhash_age_ms):
                raise ExecutionError(
                    ErrorClassification.BLOCKHASH_EXPIRED,
                    "launch blockhash exceeds configured age",
                )
            await asyncio.to_thread(
                self.store.update_launch_plan,
                plan.plan_id,
                state=LaunchState.PREPARING.value,
                blockhash=blockhash.blockhash,
                last_valid_block_height=blockhash.last_valid_block_height,
            )
            prepared = await self._prepare_all(
                request,
                plan,
                blockhash,
                estimated_cost=estimated_cost,
                execution_variant=execution_variant,
                compute_unit_limit=compute_unit_limit,
                compute_unit_price_micro_lamports=compute_unit_price_micro_lamports,
            )
            await asyncio.to_thread(
                self.store.update_launch_plan,
                plan.plan_id,
                state=LaunchState.SIGNED.value,
            )
            if plan.execution_policy == FleetExecutionPolicy.BUNDLE:
                return await self._submit_bundle(plan, prepared, estimated_cost)
            return await self._submit_components(plan, prepared)
        except asyncio.CancelledError:
            await self._persist_failure(
                plan.plan_id,
                ErrorClassification.UNKNOWN,
                "launch orchestration cancelled; inspect signed components before retry",
            )
            raise
        except Exception as error:
            classified = (
                error
                if isinstance(error, ExecutionError)
                else ExecutionError(
                    ErrorClassification.UNKNOWN,
                    f"{type(error).__name__}: {error}",
                )
            )
            await self._persist_failure(
                plan.plan_id,
                classified.classification,
                str(classified),
            )
            raise

    async def _persist_failure(
        self,
        plan_id: str,
        classification: ErrorClassification,
        reason: str,
    ) -> None:
        """Persist ambiguous signed state before propagating an orchestration error."""
        signed = await asyncio.to_thread(self.store.list_launch_components, plan_id)
        state = (
            LaunchState.RECONCILIATION_REQUIRED
            if any(item.get("signature") for item in signed)
            else LaunchState.FAILED
        )
        await asyncio.to_thread(
            self.store.update_launch_plan,
            plan_id,
            state=state.value,
            error_classification=classification.value,
            recovery_reason=reason,
        )

    async def _preflight_signers_and_balances(
        self, request: TokenLaunchRequest
    ) -> dict[str, int]:
        ids = [
            request.creator_wallet_id,
            *[item.wallet_id for item in request.additional_wallet_buys],
        ]
        missing = [wallet_id for wallet_id in ids if wallet_id not in self.wallets]
        if missing:
            raise ExecutionError(
                ErrorClassification.CONFIGURATION_ERROR,
                f"unknown fleet wallet IDs: {', '.join(missing)}",
            )
        if self.signers.public_key(request.mint_signer.signer_id) != request.mint:
            raise ExecutionError(
                ErrorClassification.SIGNING_FAILURE,
                "mint signer does not match requested mint",
            )
        balances: dict[str, int] = {}
        for wallet_id in ids:
            wallet = self.wallets[wallet_id]
            actual = self.signers.public_key(wallet.signer.signer_id)
            if actual != wallet.signer.expected_public_key:
                raise ExecutionError(
                    ErrorClassification.SIGNING_FAILURE,
                    f"signer public key mismatch for wallet {wallet_id}",
                )
            balances[wallet_id] = await self.balance_reader.native_balance_lamports(
                actual
            )
        return balances

    async def _prepare_all(
        self,
        request: TokenLaunchRequest,
        plan: LaunchExecutionPlan,
        blockhash: BlockhashContext,
        *,
        estimated_cost: LaunchCostEstimate,
        execution_variant: str,
        compute_unit_limit: int | None,
        compute_unit_price_micro_lamports: int | None,
    ) -> tuple[PreparedLaunchComponent, ...]:
        semaphore = asyncio.Semaphore(self.maximum_prepare_concurrency)
        priority_per_component = estimated_cost.priority_fees_lamports // len(
            plan.components
        )

        async def prepare(component: LaunchComponent) -> PreparedLaunchComponent:
            async with semaphore:
                item = await self.preparer.prepare(
                    request,
                    component,
                    blockhash,
                    execution_variant=execution_variant,
                    compute_unit_limit=compute_unit_limit,
                    compute_unit_price_micro_lamports=compute_unit_price_micro_lamports,
                    priority_fee_lamports=priority_per_component,
                    jito_tip_lamports=(
                        estimated_cost.bundle_tip_lamports
                        if component.sequence_index == len(plan.components) - 1
                        else 0
                    ),
                )
                await asyncio.to_thread(
                    self.store.save_launch_component,
                    plan_id=plan.plan_id,
                    component_id=component.component_id,
                    sequence_index=component.sequence_index,
                    wallet_id=component.wallet_id,
                    wallet_role=component.wallet_role.value,
                    action=component.action,
                    state=ComponentState.SIGNED.value,
                    logical_execution_id=component.logical_execution_id,
                    signature=item.transaction.signature,
                    blockhash=blockhash.blockhash,
                    quote_amount_raw=component.quote_amount_raw,
                )
                return item

        prepared = await asyncio.gather(*(prepare(item) for item in plan.components))
        return tuple(sorted(prepared, key=lambda item: item.component.sequence_index))

    async def _submit_bundle(
        self,
        plan: LaunchExecutionPlan,
        prepared: tuple[PreparedLaunchComponent, ...],
        estimated_cost: LaunchCostEstimate,
    ) -> LaunchSubmission:
        if self.bundle_submitter is None:
            raise ExecutionError(
                ErrorClassification.CONFIGURATION_ERROR,
                "bundle execution selected without a bundle-capable provider",
            )
        result = await self.bundle_submitter.submit_bundle(
            plan_id=plan.plan_id,
            transactions=tuple(item.transaction for item in prepared),
            contexts=tuple(item.context for item in prepared),
            bundle_tip_lamports=estimated_cost.bundle_tip_lamports,
        )
        if not result.accepted:
            raise ExecutionError(
                result.error_classification or ErrorClassification.BUNDLE_REJECTED,
                result.diagnostic or "bundle rejected",
            )
        await asyncio.gather(
            *(
                asyncio.to_thread(
                    self.store.update_launch_component_state,
                    plan.plan_id,
                    item.component.component_id,
                    ComponentState.SUBMITTED.value,
                )
                for item in prepared
            )
        )
        await asyncio.to_thread(
            self.store.update_launch_plan,
            plan.plan_id,
            state=LaunchState.SUBMITTED.value,
            bundle_id=result.bundle_id,
            provider_id=result.provider_id,
        )
        return LaunchSubmission(
            plan,
            LaunchState.SUBMITTED,
            result.component_signatures,
            bundle_result=result,
        )

    async def _submit_components(
        self,
        plan: LaunchExecutionPlan,
        prepared: tuple[PreparedLaunchComponent, ...],
    ) -> LaunchSubmission:
        if plan.execution_policy == FleetExecutionPolicy.PARALLEL_FAST:
            results = await asyncio.gather(
                *(
                    self.submit_component(item.transaction, item.context)
                    for item in prepared
                )
            )
        else:
            results = []
            for item in prepared:
                results.append(
                    await self.submit_component(item.transaction, item.context)
                )
                if not results[-1].accepted:
                    break
        await asyncio.gather(
            *(
                asyncio.to_thread(
                    self.store.update_launch_component_state,
                    plan.plan_id,
                    item.component.component_id,
                    (
                        ComponentState.SUBMITTED.value
                        if result.accepted
                        else ComponentState.AMBIGUOUS.value
                    ),
                )
                for item, result in zip(prepared, results, strict=False)
            )
        )
        if not all(item.accepted for item in results) or len(results) != len(prepared):
            raise ExecutionError(
                ErrorClassification.ACCEPTED_BUT_NOT_OBSERVED,
                "launch component submission was partial or ambiguous; inspect signatures",
            )
        await asyncio.to_thread(
            self.store.update_launch_plan,
            plan.plan_id,
            state=LaunchState.SUBMITTED.value,
        )
        return LaunchSubmission(
            plan,
            LaunchState.SUBMITTED,
            tuple(item.transaction.signature for item in prepared),
            submission_results=tuple(results),
        )


class TokenLaunchRecoveryService:
    """Inspect persisted launch identity after restart without resubmission."""

    def __init__(
        self,
        store: LaunchStore,
        *,
        bundle_submitter: JitoBundleSubmitter | None = None,
    ) -> None:
        self.store = store
        self.bundle_submitter = bundle_submitter

    async def recover_plan(self, plan_id: str) -> LaunchState:
        plan = await asyncio.to_thread(self.store.get_launch_plan, plan_id)
        if plan is None:
            raise KeyError(f"unknown launch plan: {plan_id}")
        state = LaunchState(plan["state"])
        if state not in {
            LaunchState.SUBMITTED,
            LaunchState.RECONCILIATION_REQUIRED,
        }:
            return state
        bundle_id = plan.get("bundle_id")
        if bundle_id:
            if self.bundle_submitter is None:
                await asyncio.to_thread(
                    self.store.update_launch_plan,
                    plan_id,
                    state=LaunchState.RECONCILIATION_REQUIRED.value,
                    recovery_reason="bundle provider unavailable during recovery",
                )
                return LaunchState.RECONCILIATION_REQUIRED
            observation = await self.bundle_submitter.observe_bundle(bundle_id)
            if observation.state == BundleObservationState.LANDED:
                components = await asyncio.to_thread(
                    self.store.list_launch_components, plan_id
                )
                await asyncio.gather(
                    *(
                        asyncio.to_thread(
                            self.store.update_launch_component_state,
                            plan_id,
                            item["component_id"],
                            ComponentState.LANDED.value,
                        )
                        for item in components
                    )
                )
                await asyncio.to_thread(
                    self.store.update_launch_plan,
                    plan_id,
                    state=LaunchState.LANDED.value,
                    recovery_reason=None,
                )
                return LaunchState.LANDED
            if observation.state in {
                BundleObservationState.FAILED,
                BundleObservationState.INVALID,
            }:
                components = await asyncio.to_thread(
                    self.store.list_launch_components, plan_id
                )
                await asyncio.gather(
                    *(
                        asyncio.to_thread(
                            self.store.update_launch_component_state,
                            plan_id,
                            item["component_id"],
                            ComponentState.FAILED.value,
                        )
                        for item in components
                    )
                )
                await asyncio.to_thread(
                    self.store.update_launch_plan,
                    plan_id,
                    state=LaunchState.FAILED.value,
                    error_classification=ErrorClassification.BUNDLE_REJECTED.value,
                    recovery_reason=observation.error,
                )
                return LaunchState.FAILED
        await asyncio.to_thread(
            self.store.update_launch_plan,
            plan_id,
            state=LaunchState.RECONCILIATION_REQUIRED.value,
            recovery_reason="submitted launch is not yet authoritatively observed",
        )
        return LaunchState.RECONCILIATION_REQUIRED
