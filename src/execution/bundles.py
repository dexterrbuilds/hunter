"""Ordered multi-transaction Jito bundle delivery."""

# Bundle validation is intentionally explicit at the transport boundary.
# ruff: noqa: PLR0913, TC001, TRY003

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic_ns

from execution.errors import ErrorClassification
from execution.ports import ExecutionContext, SignedTransaction
from execution.providers.adapters import classify_provider_response
from execution.providers.config import ProviderEndpoint, ProviderKind
from execution.providers.transport import AioHttpJsonRpcTransport, JsonRpcTransport
from execution.telemetry import utc_now
from utils.redaction import sanitize_text

# Jito's current official low-latency docs define a bundle as at most five
# transactions executed sequentially and atomically in one slot:
# https://docs.jito.wtf/lowlatencytxnsend/#bundles (verified 2026-08-30).
JITO_MAX_BUNDLE_TRANSACTIONS = 5


@dataclass(frozen=True, slots=True)
class BundleSubmissionResult:
    plan_id: str
    provider_id: str
    endpoint_id: str
    component_signatures: tuple[str, ...]
    accepted: bool
    bundle_id: str | None
    acknowledgement: str
    bytes_sent: int
    submitted_mono_ns: int
    acknowledged_mono_ns: int
    response_wall_time: object
    error_classification: ErrorClassification | None = None
    diagnostic: str | None = None

    @property
    def submit_rtt_ms(self) -> float:
        return (self.acknowledged_mono_ns - self.submitted_mono_ns) / 1_000_000


class BundleObservationState(StrEnum):
    PENDING = "pending"
    LANDED = "landed"
    FAILED = "failed"
    INVALID = "invalid"
    NOT_OBSERVED = "not_observed"


@dataclass(frozen=True, slots=True)
class BundleObservation:
    bundle_id: str
    state: BundleObservationState
    landed_slot: int | None = None
    error: str | None = None


class JitoBundleSubmitter:
    """Submit one already-signed launch/exit plan using ``sendBundle``."""

    def __init__(
        self,
        endpoint: ProviderEndpoint,
        transport: JsonRpcTransport | None = None,
    ) -> None:
        if endpoint.kind != ProviderKind.JITO:
            raise ValueError("bundle submitter requires a Jito endpoint")
        self.endpoint = endpoint
        self.transport = transport or AioHttpJsonRpcTransport()
        self._owns_transport = transport is None

    async def submit_bundle(
        self,
        *,
        plan_id: str,
        transactions: tuple[SignedTransaction, ...],
        contexts: tuple[ExecutionContext, ...],
        bundle_tip_lamports: int,
    ) -> BundleSubmissionResult:
        """Submit distinct signatures once; acknowledgement is not landing."""
        self._validate(transactions, contexts, bundle_tip_lamports)
        payload = [
            base64.b64encode(transaction.wire_bytes).decode("ascii")
            for transaction in transactions
        ]
        request = {
            "jsonrpc": "2.0",
            "id": plan_id,
            "method": "sendBundle",
            "params": [payload, {"encoding": "base64"}],
        }
        started = monotonic_ns()
        try:
            response = await self.transport.post_json(
                self.endpoint.endpoint,
                request,
                headers=self.endpoint.headers,
                timeout_ms=self.endpoint.request_timeout_ms,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            return self._failure(
                plan_id,
                transactions,
                started,
                ErrorClassification.PROVIDER_UNAVAILABLE,
                f"{type(error).__name__}: {error}",
            )
        acknowledged = monotonic_ns()
        classification = classify_provider_response(response.status, response.payload)
        if classification is not None:
            return self._failure(
                plan_id,
                transactions,
                started,
                classification,
                str(response.payload.get("error", "bundle rejected")),
                acknowledged=acknowledged,
            )
        bundle_id = response.payload.get("result")
        if not isinstance(bundle_id, str) or not bundle_id:
            return self._failure(
                plan_id,
                transactions,
                started,
                ErrorClassification.BUNDLE_REJECTED,
                "Jito returned no bundle ID",
                acknowledged=acknowledged,
            )
        return BundleSubmissionResult(
            plan_id=plan_id,
            provider_id=self.endpoint.provider_id,
            endpoint_id=self.endpoint.endpoint_id,
            component_signatures=tuple(item.signature for item in transactions),
            accepted=True,
            bundle_id=bundle_id,
            acknowledgement="bundle_id",
            bytes_sent=sum(len(item.wire_bytes) for item in transactions),
            submitted_mono_ns=started,
            acknowledged_mono_ns=acknowledged,
            response_wall_time=utc_now(),
        )

    async def observe_bundle(self, bundle_id: str) -> BundleObservation:
        """Read Jito's persisted bundle status without resubmitting it."""
        request = {
            "jsonrpc": "2.0",
            "id": bundle_id,
            "method": "getBundleStatuses",
            "params": [[bundle_id]],
        }
        response = await self.transport.post_json(
            self.endpoint.endpoint,
            request,
            headers=self.endpoint.headers,
            timeout_ms=self.endpoint.request_timeout_ms,
        )
        classification = classify_provider_response(response.status, response.payload)
        if classification is not None:
            return BundleObservation(
                bundle_id,
                BundleObservationState.NOT_OBSERVED,
                error=sanitize_text(str(response.payload.get("error"))),
            )
        result = response.payload.get("result") or {}
        values = result.get("value") if isinstance(result, dict) else None
        if not isinstance(values, list) or not values or values[0] is None:
            return BundleObservation(bundle_id, BundleObservationState.NOT_OBSERVED)
        status = values[0]
        if not isinstance(status, dict):
            return BundleObservation(bundle_id, BundleObservationState.NOT_OBSERVED)
        confirmation = str(status.get("confirmation_status", "")).lower()
        error = status.get("err")
        if error is not None:
            return BundleObservation(
                bundle_id,
                BundleObservationState.FAILED,
                error=sanitize_text(str(error)),
            )
        if confirmation in {"processed", "confirmed", "finalized"}:
            slot = status.get("slot")
            return BundleObservation(
                bundle_id,
                BundleObservationState.LANDED,
                landed_slot=slot if isinstance(slot, int) else None,
            )
        return BundleObservation(bundle_id, BundleObservationState.PENDING)

    def _validate(
        self,
        transactions: tuple[SignedTransaction, ...],
        contexts: tuple[ExecutionContext, ...],
        tip: int,
    ) -> None:
        if not transactions or len(transactions) > JITO_MAX_BUNDLE_TRANSACTIONS:
            raise ValueError("Jito bundles require between one and five transactions")
        if len(transactions) != len(contexts):
            raise ValueError("each bundle transaction requires an execution context")
        signatures = [item.signature for item in transactions]
        if len(signatures) != len(set(signatures)):
            raise ValueError("wallet-fleet bundle signatures must be distinct")
        blockhashes = {item.blockhash.blockhash for item in contexts}
        if len(blockhashes) != 1:
            raise ValueError("bundle components must use one blockhash strategy")
        if tip < self.endpoint.minimum_tip_lamports:
            raise ValueError("bundle tip is below the configured minimum")
        if (
            self.endpoint.maximum_tip_lamports
            and tip > self.endpoint.maximum_tip_lamports
        ):
            raise ValueError("bundle tip exceeds the configured maximum")
        context_tips = [item.jito_tip_lamports for item in contexts]
        if sum(context_tips) != tip:
            raise ValueError("bundle tip must appear exactly once across components")
        if sum(value > 0 for value in context_tips) > 1:
            raise ValueError("bundle contains more than one Jito tip")

    def _failure(
        self,
        plan_id: str,
        transactions: tuple[SignedTransaction, ...],
        started: int,
        classification: ErrorClassification,
        diagnostic: str,
        *,
        acknowledged: int | None = None,
    ) -> BundleSubmissionResult:
        return BundleSubmissionResult(
            plan_id=plan_id,
            provider_id=self.endpoint.provider_id,
            endpoint_id=self.endpoint.endpoint_id,
            component_signatures=tuple(item.signature for item in transactions),
            accepted=False,
            bundle_id=None,
            acknowledgement="error",
            bytes_sent=sum(len(item.wire_bytes) for item in transactions),
            submitted_mono_ns=started,
            acknowledged_mono_ns=acknowledged or monotonic_ns(),
            response_wall_time=utc_now(),
            error_classification=classification,
            diagnostic=sanitize_text(diagnostic)[:2_048],
        )

    async def close(self) -> None:
        if self._owns_transport:
            await self.transport.close()
