"""Provider-specific signed-transaction submission adapters."""

# Response normalization is intentionally centralized and branch-heavy.
# ruff: noqa: C901, PLR0911, PLR0913, TRY003

from __future__ import annotations

import asyncio
import base64
from time import monotonic_ns
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from execution.errors import ErrorClassification
from execution.ports import (
    ExecutionContext,
    SignedTransaction,
    SubmissionResult,
)
from execution.providers.config import ProviderEndpoint, ProviderKind
from execution.providers.transport import AioHttpJsonRpcTransport, JsonRpcTransport
from execution.telemetry import utc_now
from utils.redaction import sanitize_text

HTTP_UNAUTHORIZED = {401, 403}
HTTP_RATE_LIMIT = 429
HTTP_UNAVAILABLE = {502, 503, 504}
HTTP_SERVER_ERROR_MINIMUM = 500
JITO_BUNDLE_MINIMUM_TIP_LAMPORTS = 1_000
MAXIMUM_DIAGNOSTIC_CHARACTERS = 2_048


class JsonRpcTransactionSubmitter:
    """Standard Solana JSON-RPC ``sendTransaction`` adapter."""

    def __init__(
        self,
        endpoint: ProviderEndpoint,
        transport: JsonRpcTransport | None = None,
    ) -> None:
        if endpoint.kind != ProviderKind.STANDARD_RPC:
            raise ValueError("standard submitter requires a standard_rpc endpoint")
        self.endpoint = endpoint
        self.transport = transport or AioHttpJsonRpcTransport()
        self._owns_transport = transport is None

    @property
    def provider_id(self) -> str:
        return self.endpoint.provider_id

    def _request(self, transaction: SignedTransaction) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": transaction.signature,
            "method": "sendTransaction",
            "params": [
                base64.b64encode(transaction.wire_bytes).decode("ascii"),
                {
                    "encoding": "base64",
                    "skipPreflight": self.endpoint.skip_preflight,
                    "maxRetries": self.endpoint.max_retries,
                },
            ],
        }

    async def submit(
        self,
        transaction: SignedTransaction,
        execution_context: ExecutionContext,
    ) -> SubmissionResult:
        return await self._submit_request(
            transaction,
            execution_context,
            self._request(transaction),
            acknowledgement="signature",
        )

    async def _submit_request(
        self,
        transaction: SignedTransaction,
        execution_context: ExecutionContext,
        request: dict[str, Any],
        *,
        acknowledgement: str,
    ) -> SubmissionResult:
        started = monotonic_ns()
        try:
            response = await self.transport.post_json(
                self._request_endpoint(),
                request,
                headers=self.endpoint.headers,
                timeout_ms=self.endpoint.request_timeout_ms,
            )
        except TimeoutError as error:
            return self._failure(
                transaction,
                execution_context,
                started,
                ErrorClassification.RPC_TRANSPORT_FAILURE,
                "provider request timed out",
                code=type(error).__name__,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            return self._failure(
                transaction,
                execution_context,
                started,
                ErrorClassification.PROVIDER_UNAVAILABLE,
                _safe_diagnostic(f"{type(error).__name__}: {error}"),
            )

        acknowledged = monotonic_ns()
        classification = classify_provider_response(response.status, response.payload)
        if classification is not None:
            code, detail = _response_error(response.payload)
            if classification == ErrorClassification.DUPLICATE_SIGNATURE:
                return SubmissionResult(
                    signature=transaction.signature,
                    provider_id=self.provider_id,
                    endpoint_id=self.endpoint.endpoint_id,
                    execution_variant=execution_context.execution_variant,
                    accepted=True,
                    acknowledgement="duplicate_signature",
                    bytes_sent=len(transaction.wire_bytes),
                    connection_reused=response.connection_reused,
                    connection_session_generation=response.session_generation,
                    connection_session_created=response.session_created_for_request,
                    provider_reference=_provider_reference(response.headers),
                    submit_started_mono_ns=started,
                    acknowledged_mono_ns=acknowledged,
                    response_wall_time=utc_now(),
                    error_classification=classification,
                    error_code=code or response.status,
                    diagnostic=_safe_diagnostic(detail),
                )
            return SubmissionResult(
                signature=transaction.signature,
                provider_id=self.provider_id,
                endpoint_id=self.endpoint.endpoint_id,
                execution_variant=execution_context.execution_variant,
                accepted=False,
                acknowledgement="error",
                bytes_sent=len(transaction.wire_bytes),
                connection_reused=response.connection_reused,
                connection_session_generation=response.session_generation,
                connection_session_created=response.session_created_for_request,
                provider_reference=_provider_reference(response.headers),
                submit_started_mono_ns=started,
                acknowledged_mono_ns=acknowledged,
                response_wall_time=utc_now(),
                error_classification=classification,
                error_code=code or response.status,
                diagnostic=_safe_diagnostic(detail),
            )

        signature = response.payload.get("result")
        if not isinstance(signature, str) or not signature:
            return self._failure(
                transaction,
                execution_context,
                started,
                ErrorClassification.RPC_REJECTION,
                "provider returned no transaction signature",
                acknowledged=acknowledged,
            )
        if signature != transaction.signature:
            return self._failure(
                transaction,
                execution_context,
                started,
                ErrorClassification.RPC_REJECTION,
                "provider returned a signature different from the signed transaction",
                acknowledged=acknowledged,
            )
        return SubmissionResult(
            signature=signature,
            provider_id=self.provider_id,
            endpoint_id=self.endpoint.endpoint_id,
            execution_variant=execution_context.execution_variant,
            accepted=True,
            acknowledgement=acknowledgement,
            bytes_sent=len(transaction.wire_bytes),
            connection_reused=response.connection_reused,
            connection_session_generation=response.session_generation,
            connection_session_created=response.session_created_for_request,
            provider_reference=_provider_reference(response.headers),
            submit_started_mono_ns=started,
            acknowledged_mono_ns=acknowledged,
            response_wall_time=utc_now(),
        )

    def _failure(
        self,
        transaction: SignedTransaction,
        execution_context: ExecutionContext,
        started: int,
        classification: ErrorClassification,
        detail: str,
        *,
        code: str | int | None = None,
        acknowledged: int | None = None,
    ) -> SubmissionResult:
        return SubmissionResult(
            signature=transaction.signature,
            provider_id=self.provider_id,
            endpoint_id=self.endpoint.endpoint_id,
            execution_variant=execution_context.execution_variant,
            accepted=False,
            acknowledgement="error",
            bytes_sent=len(transaction.wire_bytes),
            submit_started_mono_ns=started,
            acknowledged_mono_ns=acknowledged or monotonic_ns(),
            response_wall_time=utc_now(),
            error_classification=classification,
            error_code=code,
            diagnostic=_safe_diagnostic(detail),
        )

    async def warm(self) -> bool:
        """Warm a documented provider endpoint when one is configured."""
        timeout_ms = min(self.endpoint.request_timeout_ms, 2_000)
        if self.endpoint.warmup_endpoint is not None:
            return await self.transport.warm(
                self.endpoint.warmup_endpoint,
                timeout_ms=timeout_ms,
            )
        if self.endpoint.kind != ProviderKind.STANDARD_RPC:
            return False
        response = await self.transport.post_json(
            self.endpoint.endpoint,
            {
                "jsonrpc": "2.0",
                "id": "hunter-connection-warmup",
                "method": "getHealth",
            },
            headers=self.endpoint.headers,
            timeout_ms=timeout_ms,
        )
        return response.status < HTTP_SERVER_ERROR_MINIMUM

    def _request_endpoint(self) -> str:
        return self.endpoint.endpoint

    async def close(self) -> None:
        if self._owns_transport:
            await self.transport.close()


class HeliusSenderSubmitter(JsonRpcTransactionSubmitter):
    """Helius Sender adapter for a separately signed, tipped variant."""

    def __init__(
        self,
        endpoint: ProviderEndpoint,
        transport: JsonRpcTransport | None = None,
    ) -> None:
        if endpoint.kind != ProviderKind.HELIUS_SENDER:
            raise ValueError("Helius submitter requires a helius_sender endpoint")
        self.endpoint = endpoint
        self.transport = transport or AioHttpJsonRpcTransport()
        self._owns_transport = transport is None

    async def submit(
        self,
        transaction: SignedTransaction,
        execution_context: ExecutionContext,
    ) -> SubmissionResult:
        invalid = _validate_tipped_variant(
            transaction,
            execution_context,
            endpoint=self.endpoint,
            require_priority_fee=True,
            allowed_variants={"helius_sender_tipped", "jito_tipped"},
        )
        if invalid is not None:
            return invalid
        request = {
            "jsonrpc": "2.0",
            "id": transaction.signature,
            "method": "sendTransaction",
            "params": [
                base64.b64encode(transaction.wire_bytes).decode("ascii"),
                {"encoding": "base64", "skipPreflight": True, "maxRetries": 0},
            ],
        }
        return await self._submit_request(
            transaction,
            execution_context,
            request,
            acknowledgement="helius_sender_signature",
        )


class JitoTransactionSubmitter(JsonRpcTransactionSubmitter):
    """Jito Block Engine single-transaction adapter.

    ``bundle_only`` uses Jito's documented query mode and is deliberately
    reported as a distinct execution variant. Multi-transaction bundles are
    not constructed by this adapter.
    """

    def __init__(
        self,
        endpoint: ProviderEndpoint,
        transport: JsonRpcTransport | None = None,
    ) -> None:
        if endpoint.kind != ProviderKind.JITO:
            raise ValueError("Jito submitter requires a jito endpoint")
        self.endpoint = endpoint
        self.transport = transport or AioHttpJsonRpcTransport()
        self._owns_transport = transport is None

    async def submit(
        self,
        transaction: SignedTransaction,
        execution_context: ExecutionContext,
    ) -> SubmissionResult:
        if execution_context.jito_tip_lamports:
            invalid = _validate_tipped_variant(
                transaction,
                execution_context,
                endpoint=self.endpoint,
                require_priority_fee=False,
                allowed_variants={"jito_tipped", "helius_sender_tipped"},
            )
            if invalid is not None:
                return invalid
        elif self.endpoint.bundle_only:
            return _validation_failure(
                transaction,
                execution_context,
                self.endpoint,
                ErrorClassification.TIP_TOO_LOW,
                "Jito bundleOnly requires a tipped transaction variant",
            )

        request = {
            "jsonrpc": "2.0",
            "id": transaction.signature,
            "method": "sendTransaction",
            "params": [
                base64.b64encode(transaction.wire_bytes).decode("ascii"),
                {"encoding": "base64"},
            ],
        }
        result = await self._submit_request(
            transaction,
            execution_context,
            request,
            acknowledgement=(
                "jito_bundle_only_signature"
                if self.endpoint.bundle_only
                else "jito_signature"
            ),
        )
        return result

    def _request_endpoint(self) -> str:
        if not self.endpoint.bundle_only:
            return self.endpoint.endpoint
        parts = urlsplit(self.endpoint.endpoint)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["bundleOnly"] = "true"
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )


def classify_provider_response(
    status: int, payload: dict[str, Any]
) -> ErrorClassification | None:
    """Normalize HTTP and JSON-RPC failures without provider credentials."""
    if status in HTTP_UNAUTHORIZED:
        return ErrorClassification.PROVIDER_AUTHENTICATION_FAILURE
    if status == HTTP_RATE_LIMIT:
        return ErrorClassification.RPC_RATE_LIMIT
    if status in HTTP_UNAVAILABLE or status >= HTTP_SERVER_ERROR_MINIMUM:
        return ErrorClassification.PROVIDER_UNAVAILABLE
    error = payload.get("error")
    if not error:
        return None
    code, detail = _response_error(payload)
    lower = detail.lower()
    if code == HTTP_RATE_LIMIT or "rate limit" in lower or "too many requests" in lower:
        return ErrorClassification.RPC_RATE_LIMIT
    if code in {401, 403} or "unauthorized" in lower or "authentication" in lower:
        return ErrorClassification.PROVIDER_AUTHENTICATION_FAILURE
    if "already processed" in lower or "duplicate signature" in lower:
        return ErrorClassification.DUPLICATE_SIGNATURE
    if "tip" in lower and ("low" in lower or "minimum" in lower):
        return ErrorClassification.TIP_TOO_LOW
    if "bundle" in lower:
        return ErrorClassification.BUNDLE_REJECTED
    if "leader" in lower or "route" in lower:
        return ErrorClassification.LEADER_ROUTING_FAILURE
    return ErrorClassification.RPC_REJECTION


def safe_fallback_failure(classification: ErrorClassification | None) -> bool:
    """Whether another transport may relay the *same* signed transaction."""
    return classification in {
        ErrorClassification.RPC_TRANSPORT_FAILURE,
        ErrorClassification.RPC_RATE_LIMIT,
        ErrorClassification.PROVIDER_UNAVAILABLE,
        ErrorClassification.PROVIDER_AUTHENTICATION_FAILURE,
        ErrorClassification.LEADER_ROUTING_FAILURE,
    }


def _validate_tipped_variant(
    transaction: SignedTransaction,
    context: ExecutionContext,
    *,
    endpoint: ProviderEndpoint,
    require_priority_fee: bool,
    allowed_variants: set[str],
) -> SubmissionResult | None:
    tip = context.jito_tip_lamports
    minimum = endpoint.minimum_tip_lamports
    if endpoint.bundle_only:
        minimum = max(minimum, JITO_BUNDLE_MINIMUM_TIP_LAMPORTS)
    if context.execution_variant not in allowed_variants:
        return _validation_failure(
            transaction,
            context,
            endpoint,
            ErrorClassification.CONFIGURATION_ERROR,
            "provider requires an explicitly tipped execution variant",
        )
    if tip < minimum:
        return _validation_failure(
            transaction,
            context,
            endpoint,
            ErrorClassification.TIP_TOO_LOW,
            f"configured tip {tip} is below provider minimum {minimum}",
        )
    if endpoint.maximum_tip_lamports and tip > endpoint.maximum_tip_lamports:
        return _validation_failure(
            transaction,
            context,
            endpoint,
            ErrorClassification.RISK_LIMIT_EXCEEDED,
            "configured tip exceeds provider maximum",
        )
    if context.metadata.get("tip_instruction_count") != 1:
        return _validation_failure(
            transaction,
            context,
            endpoint,
            ErrorClassification.CONFIGURATION_ERROR,
            "tipped variant must contain exactly one tip instruction",
        )
    if require_priority_fee and not context.compute_unit_price_micro_lamports:
        return _validation_failure(
            transaction,
            context,
            endpoint,
            ErrorClassification.CONFIGURATION_ERROR,
            "provider requires a positive compute-unit price",
        )
    return None


def _validation_failure(
    transaction: SignedTransaction,
    context: ExecutionContext,
    endpoint: ProviderEndpoint,
    classification: ErrorClassification,
    detail: str,
) -> SubmissionResult:
    now = monotonic_ns()
    return SubmissionResult(
        signature=transaction.signature,
        provider_id=endpoint.provider_id,
        endpoint_id=endpoint.endpoint_id,
        execution_variant=context.execution_variant,
        accepted=False,
        acknowledgement="validation_error",
        bytes_sent=0,
        submit_started_mono_ns=now,
        acknowledged_mono_ns=now,
        response_wall_time=utc_now(),
        error_classification=classification,
        diagnostic=detail,
    )


def _response_error(payload: dict[str, Any]) -> tuple[str | int | None, str]:
    error = payload.get("error")
    if isinstance(error, dict):
        return error.get("code"), str(error.get("message", error))
    return None, str(error or "provider rejected transaction")


def _provider_reference(headers: dict[str, str]) -> str | None:
    for key, value in headers.items():
        if key.lower() in {"x-bundle-id", "bundle-id"}:
            return _safe_diagnostic(value, maximum=256)
    return None


def _safe_diagnostic(
    value: object, *, maximum: int = MAXIMUM_DIAGNOSTIC_CHARACTERS
) -> str:
    return sanitize_text(value)[:maximum]
