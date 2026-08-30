"""Controlled multi-provider broadcasting for one signed transaction identity."""

# Public configuration failures use direct, actionable exception messages.
# ruff: noqa: TRY003

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from execution.errors import ErrorClassification
from execution.health import ProviderHealthRegistry
from execution.ports import (
    ExecutionContext,
    SignedTransaction,
    SubmissionResult,
    TransactionSubmitter,
)
from execution.providers.adapters import safe_fallback_failure
from execution.providers.config import BroadcastMode

AttemptCallback = Callable[[SubmissionResult], Awaitable[None] | None]


class SubmissionRouter:
    """Routes the same wire bytes/signature without creating replacement trades."""

    def __init__(
        self,
        submitters: Sequence[TransactionSubmitter],
        *,
        mode: BroadcastMode = BroadcastMode.SINGLE,
        hedge_delay_ms: int = 75,
        attempt_callback: AttemptCallback | None = None,
        health: ProviderHealthRegistry | None = None,
    ) -> None:
        if not submitters:
            raise ValueError("submission router requires at least one provider")
        if hedge_delay_ms < 0:
            raise ValueError("hedge delay must be non-negative")
        self.submitters = tuple(submitters)
        self.mode = mode
        self.hedge_delay_ms = hedge_delay_ms
        self.attempt_callback = attempt_callback
        self.health = health or ProviderHealthRegistry()
        self._background_tasks: set[asyncio.Task[SubmissionResult]] = set()
        self._record_tasks: set[asyncio.Task[None]] = set()

    @property
    def provider_id(self) -> str:
        return f"router:{self.mode.value}"

    async def submit(
        self,
        transaction: SignedTransaction,
        execution_context: ExecutionContext,
    ) -> SubmissionResult:
        if transaction.signature != execution_context.signature:
            raise ValueError("execution context signature differs from wire identity")
        ordered = self._ordered_submitters()
        self._validate_variant(ordered, execution_context.execution_variant)
        if self.mode == BroadcastMode.SINGLE:
            return await self._submit_one(ordered[0], transaction, execution_context)
        if self.mode == BroadcastMode.FALLBACK:
            return await self._fallback(transaction, execution_context, ordered)
        if self.mode == BroadcastMode.RACE:
            return await self._race(self.submitters, transaction, execution_context)
        if self.mode == BroadcastMode.HEDGED:
            return await self._hedged(transaction, execution_context, ordered)
        raise ValueError(f"unsupported broadcast mode: {self.mode}")

    def _validate_variant(
        self,
        submitters: Sequence[TransactionSubmitter],
        variant: str,
    ) -> None:
        candidates = submitters[:1] if self.mode == BroadcastMode.SINGLE else submitters
        capabilities = [getattr(item, "capabilities", None) for item in candidates]
        known = [item for item in capabilities if item is not None]
        if any(not item.accepts_variant(variant) for item in known):
            raise ValueError(
                f"configured provider does not accept execution variant {variant}"
            )
        if self.mode not in {BroadcastMode.RACE, BroadcastMode.HEDGED}:
            return
        if len(known) != len(candidates):
            return
        first = known[0]
        if any(
            not first.race_compatible_with(item, variant=variant) for item in known[1:]
        ):
            raise ValueError("broadcast race contains incompatible execution variants")

    async def _submit_one(
        self,
        submitter: TransactionSubmitter,
        transaction: SignedTransaction,
        context: ExecutionContext,
    ) -> SubmissionResult:
        result = await submitter.submit(transaction, context)
        await self._record(result)
        return result

    async def _fallback(
        self,
        transaction: SignedTransaction,
        context: ExecutionContext,
        submitters: Sequence[TransactionSubmitter],
    ) -> SubmissionResult:
        last: SubmissionResult | None = None
        for submitter in submitters:
            last = await self._submit_one(submitter, transaction, context)
            if last.acceptable_acknowledgement:
                return last
            if not safe_fallback_failure(last.error_classification):
                return last
        if last is None:
            raise RuntimeError("fallback routing had no providers")
        return last

    async def _race(
        self,
        submitters: Sequence[TransactionSubmitter],
        transaction: SignedTransaction,
        context: ExecutionContext,
    ) -> SubmissionResult:
        tasks = [
            asyncio.create_task(
                submitter.submit(transaction, context),
                name=f"hunter-submit-{submitter.provider_id}",
            )
            for submitter in submitters
        ]
        failures: list[SubmissionResult] = []
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                result = task.result()
                await self._record(result)
                if result.acceptable_acknowledgement:
                    for remainder in pending:
                        self._track_background(remainder)
                    return result
                failures.append(result)
        if not failures:
            raise RuntimeError("race routing produced no provider result")
        return failures[-1]

    async def _hedged(
        self,
        transaction: SignedTransaction,
        context: ExecutionContext,
        submitters: Sequence[TransactionSubmitter],
    ) -> SubmissionResult:
        primary = asyncio.create_task(
            submitters[0].submit(transaction, context),
            name=f"hunter-submit-{submitters[0].provider_id}",
        )
        try:
            result = await asyncio.wait_for(
                asyncio.shield(primary), timeout=self.hedge_delay_ms / 1_000
            )
        except TimeoutError:
            if len(submitters) == 1:
                result = await primary
                await self._record(result)
                return result
            return await self._race(
                (self._task_submitter(primary), *submitters[1:]),
                transaction,
                context,
            )
        await self._record(result)
        if result.acceptable_acknowledgement or len(submitters) == 1:
            return result
        if safe_fallback_failure(result.error_classification):
            return await self._race(submitters[1:], transaction, context)
        return result

    def _ordered_submitters(self) -> tuple[TransactionSubmitter, ...]:
        by_id = {item.provider_id: item for item in self.submitters}
        ordered_ids = self.health.order_provider_ids(list(by_id))
        return tuple(by_id[provider_id] for provider_id in ordered_ids)

    @staticmethod
    def _task_submitter(
        task: asyncio.Task[SubmissionResult],
    ) -> TransactionSubmitter:
        return _ExistingTaskSubmitter(task)

    def _track_background(self, task: asyncio.Task[SubmissionResult]) -> None:
        self._background_tasks.add(task)

        def done(completed: asyncio.Task[SubmissionResult]) -> None:
            self._background_tasks.discard(completed)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is None:
                record_task = asyncio.create_task(
                    self._record(completed.result()),
                    name="hunter-record-provider-attempt",
                )
                self._record_tasks.add(record_task)
                record_task.add_done_callback(self._record_tasks.discard)

        task.add_done_callback(done)

    async def _record(self, result: SubmissionResult) -> None:
        self.health.record_submission(result)
        if self.attempt_callback is not None:
            returned = self.attempt_callback(result)
            if returned is not None:
                await returned

    async def drain(self) -> None:
        """Wait for telemetry-only race attempts without affecting hot-path return."""
        if self._background_tasks:
            await asyncio.gather(*tuple(self._background_tasks), return_exceptions=True)
        if self._record_tasks:
            await asyncio.gather(*tuple(self._record_tasks), return_exceptions=True)

    async def warm(self) -> dict[str, bool]:
        """Warm configured provider connections without submitting transactions."""

        async def warm_one(
            submitter: TransactionSubmitter,
        ) -> tuple[str, bool]:
            warm = getattr(submitter, "warm", None)
            if warm is None:
                return submitter.provider_id, False
            try:
                return submitter.provider_id, bool(await warm())
            except Exception:  # noqa: BLE001 - warm-up is advisory only
                return submitter.provider_id, False

        pairs = await asyncio.gather(*(warm_one(item) for item in self.submitters))
        return dict(pairs)

    async def close(self) -> None:
        await self.drain()
        await asyncio.gather(
            *(submitter.close() for submitter in self.submitters),
            return_exceptions=True,
        )


class _ExistingTaskSubmitter:
    """Adapts an already-running primary task into the race implementation."""

    def __init__(self, task: asyncio.Task[SubmissionResult]) -> None:
        self.task = task

    @property
    def provider_id(self) -> str:
        return "hedged-primary"

    async def submit(
        self,
        transaction: SignedTransaction,
        execution_context: ExecutionContext,
    ) -> SubmissionResult:
        del transaction, execution_context
        return await self.task

    async def close(self) -> None:
        return None


def unsupported_provider_result(
    transaction: SignedTransaction,
    context: ExecutionContext,
    provider_id: str,
    endpoint_id: str,
) -> SubmissionResult:
    """Fail closed for configured provider types Hunter does not implement."""
    return SubmissionResult(
        transaction.signature,
        provider_id,
        endpoint_id,
        execution_variant=context.execution_variant,
        accepted=False,
        acknowledgement="unsupported_provider",
        error_classification=ErrorClassification.UNSUPPORTED_PROVIDER,
        diagnostic="configured execution provider is unsupported",
    )
