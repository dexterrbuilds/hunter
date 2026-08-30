"""Controlled benchmark orchestration with no dependency on trading strategy."""

# Protocol-shaped interfaces and explicit safety errors are intentional here.
# ruff: noqa: PLC0415, PLR0913, TC001, TRY003

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from time import monotonic, monotonic_ns
from typing import Protocol
from uuid import uuid4

import aiohttp

from benchmark.live_config import ExitPolicy, LiveBenchmarkConfig
from benchmark.live_models import (
    BenchmarkAttempt,
    BenchmarkKind,
    ConnectionState,
    DetectionObservation,
)
from benchmark.live_store import BenchmarkStore
from execution.errors import ErrorClassification, ExecutionError
from execution.providers.config import ExecutionRoutingConfig, ProviderRole
from interfaces.core import TokenInfo
from utils.redaction import sanitize_text

HTTP_RATE_LIMITED = 429
HTTP_AUTH_FAILURES = {401, 403}


@dataclass(frozen=True, slots=True)
class EconomicOutcome:
    """Normalized result returned by the existing Hunter execution path."""

    success: bool
    signature: str | None
    execution_variant: str
    quote_spent_raw: int | None = None
    error_classification: ErrorClassification | None = None
    error_detail: str | None = None
    telemetry_records: tuple[dict[str, object], ...] = ()
    reused_existing_signature: bool = False


@dataclass(frozen=True, slots=True)
class EconomicTrialOutcome:
    buy: EconomicOutcome
    exit: EconomicOutcome | None = None


class EconomicExecutor(Protocol):
    async def execute_buy(
        self,
        *,
        mint: str,
        logical_trade_id: str,
        route_id: str,
        provider_ids: tuple[str, ...],
        mode: str,
        execution_variant: str,
    ) -> EconomicOutcome: ...

    async def execute_exit(
        self, *, mint: str, logical_trade_id: str
    ) -> EconomicOutcome: ...


class ReadProbe(Protocol):
    async def __call__(self) -> object: ...


class LiveBenchmarkSession:
    """Enforce authorization, caps, duration, and duplicate-trade safety."""

    def __init__(
        self,
        config: LiveBenchmarkConfig,
        store: BenchmarkStore,
        *,
        cli_allow_live: bool,
        risk_enforced: bool,
        session_id: str | None = None,
    ) -> None:
        config.authorize(cli_allow_live=cli_allow_live, risk_enforced=risk_enforced)
        self.config = config
        self.store = store
        self.session_id = session_id or f"live-{uuid4()}"
        self.started_mono = monotonic()
        self.store.create_session(
            self.session_id,
            BenchmarkKind.ECONOMIC,
            region_label=config.region_label,
            live_authorized=True,
            dedicated_wallet=config.dedicated_wallet,
            metadata={
                "mint": config.mint,
                "quote_mint": config.quote_mint,
                "provider_matrix": [asdict(route) for route in config.provider_matrix],
                "exit_policy": config.exit_policy.value,
                "providers_warmed": config.warm_providers,
            },
        )

    def assert_within_limits(self, *, upcoming_spend_raw: int | None = None) -> None:
        elapsed = monotonic() - self.started_mono
        if elapsed > self.config.caps.maximum_duration_seconds:
            raise TimeoutError("maximum live benchmark duration exceeded")
        count, spend = self.store.economic_totals(self.session_id)
        if count >= self.config.caps.maximum_live_trades:
            raise PermissionError("maximum live benchmark trade count reached")
        if (
            spend
            + (
                self.config.maximum_per_trade_exposure_raw
                if upcoming_spend_raw is None
                else upcoming_spend_raw
            )
            > self.config.caps.maximum_cumulative_spend_raw
        ):
            raise PermissionError(
                "maximum cumulative benchmark spend would be exceeded"
            )

    async def execute(
        self, executor: EconomicExecutor, *, route_id: str
    ) -> EconomicTrialOutcome:
        """Run one explicitly selected route through Hunter's executor."""
        route = next(
            (item for item in self.config.provider_matrix if item.route_id == route_id),
            None,
        )
        if route is None:
            raise ValueError(f"benchmark route is not configured: {route_id}")
        try:
            self.assert_within_limits()
        except (PermissionError, TimeoutError, ValueError) as error:
            self._record_local_failure(route.route_id, route.mode, error)
            raise
        # Stable across command restarts: the position store can inspect an
        # ambiguous prior signature before constructing any replacement.
        logical_id = (
            f"benchmark:{route.route_id}:{route.execution_variant}:{self.config.mint}"
        )
        variant = route.execution_variant
        self.store.reserve_economic_trial(
            session_id=self.session_id,
            logical_trade_id=logical_id,
            execution_variant=variant,
            mint=self.config.mint,
            quote_spend_raw=self.config.maximum_per_trade_exposure_raw,
        )
        try:
            outcome = await asyncio.wait_for(
                executor.execute_buy(
                    mint=self.config.mint,
                    logical_trade_id=logical_id,
                    route_id=route.route_id,
                    provider_ids=route.providers,
                    mode=route.mode,
                    execution_variant=route.execution_variant,
                ),
                timeout=self.config.caps.maximum_duration_seconds,
            )
        except Exception as error:
            self.store.update_economic_trial(
                self.session_id,
                logical_id,
                variant,
                state="failed_before_result",
            )
            self._record_local_failure(route.route_id, route.mode, error)
            raise RuntimeError(sanitize_text(str(error))) from error
        state = "confirmed" if outcome.success else "failed_or_ambiguous"
        self.store.update_economic_trial(
            self.session_id,
            logical_id,
            variant,
            state=state,
            signature=outcome.signature,
        )
        self._persist_economic_telemetry(outcome, route.mode, route.route_id)
        exit_outcome = None
        if outcome.success and self.config.exit_policy != ExitPolicy.MANUAL:
            if self.config.exit_policy == ExitPolicy.AFTER_SECONDS:
                await asyncio.sleep(self.config.exit_after_seconds or 0)
            try:
                self.assert_duration_only()
                self.assert_within_limits(upcoming_spend_raw=0)
            except (PermissionError, TimeoutError, ValueError) as error:
                self._record_local_failure(route.route_id, route.mode, error)
                raise
            exit_id = f"{logical_id}:exit"
            self.store.reserve_economic_trial(
                session_id=self.session_id,
                logical_trade_id=exit_id,
                execution_variant="exit",
                mint=self.config.mint,
                quote_spend_raw=0,
            )
            try:
                exit_outcome = await executor.execute_exit(
                    mint=self.config.mint,
                    logical_trade_id=exit_id,
                )
            except Exception as error:
                self.store.update_economic_trial(
                    self.session_id,
                    exit_id,
                    "exit",
                    state="failed_before_result",
                )
                self._record_local_failure(route.route_id, route.mode, error)
                raise RuntimeError(sanitize_text(str(error))) from error
            self.store.update_economic_trial(
                self.session_id,
                exit_id,
                "exit",
                state=("confirmed" if exit_outcome.success else "failed_or_ambiguous"),
                signature=exit_outcome.signature,
            )
            self._persist_economic_telemetry(exit_outcome, route.mode, route.route_id)
        return EconomicTrialOutcome(outcome, exit_outcome)

    def assert_duration_only(self) -> None:
        if monotonic() - self.started_mono > self.config.caps.maximum_duration_seconds:
            raise TimeoutError("maximum live benchmark duration exceeded")

    def _record_local_failure(
        self, route_id: str, route_mode: str, error: Exception
    ) -> None:
        classification = getattr(error, "classification", None)
        if not isinstance(classification, ErrorClassification):
            classification = (
                ErrorClassification.RISK_LIMIT_EXCEEDED
                if isinstance(error, PermissionError | TimeoutError)
                else ErrorClassification.CONFIGURATION_ERROR
            )
        self.store.record_attempt(
            BenchmarkAttempt(
                session_id=self.session_id,
                attempt_id=f"hunter-rejection:{uuid4()}",
                kind=BenchmarkKind.ECONOMIC,
                provider_id="hunter",
                endpoint_id="local",
                route_mode=route_mode,
                route_id=route_id,
                connection_state=ConnectionState.WARM,
                success=False,
                error_classification=classification,
                details={"error": sanitize_text(str(error))},
            )
        )

    def _persist_economic_telemetry(
        self, outcome: EconomicOutcome, route_mode: str, route_id: str
    ) -> None:
        ambiguous_values = {
            ErrorClassification.ACCEPTED_BUT_NOT_OBSERVED.value,
            ErrorClassification.CONFIRMATION_TIMEOUT.value,
            ErrorClassification.TRANSACTION_DROPPED.value,
        }
        if not outcome.telemetry_records:
            self.store.record_attempt(
                BenchmarkAttempt(
                    session_id=self.session_id,
                    attempt_id=f"execution-without-new-transport:{uuid4()}",
                    kind=BenchmarkKind.ECONOMIC,
                    provider_id="hunter",
                    endpoint_id="local",
                    route_mode=route_mode,
                    route_id=route_id,
                    connection_state=ConnectionState.WARM,
                    success=outcome.success,
                    ambiguous=(
                        outcome.error_classification is not None
                        and outcome.error_classification.value in ambiguous_values
                    ),
                    error_classification=outcome.error_classification,
                    signature=outcome.signature,
                    execution_variant=outcome.execution_variant,
                    details={
                        "reused_existing_signature": outcome.reused_existing_signature,
                        "detail": sanitize_text(outcome.error_detail or ""),
                    },
                )
            )
            return
        for telemetry in outcome.telemetry_records:
            for index, provider_attempt in enumerate(
                telemetry.get("provider_attempts", [])
            ):
                if not isinstance(provider_attempt, dict):
                    continue
                classification_value = provider_attempt.get(
                    "error_classification"
                ) or telemetry.get("error_classification")
                classification = (
                    ErrorClassification(classification_value)
                    if classification_value
                    else None
                )
                submit_started = provider_attempt.get("submit_started_mono_ns")
                accepted = bool(provider_attempt.get("accepted"))
                processed = telemetry.get("processed_mono_ns")
                detected = telemetry.get("detected_mono_ns")
                state = _connection_state(provider_attempt)
                launch_to_detection = _wall_elapsed_ms(
                    self.config.authoritative_launch_timestamp,
                    telemetry.get("event_observed_at"),
                )
                detection_to_landed = _elapsed_ms(detected, processed)
                self.store.record_attempt(
                    BenchmarkAttempt(
                        session_id=self.session_id,
                        attempt_id=(
                            f"{telemetry.get('execution_id', 'execution')}:{index}"
                        ),
                        kind=BenchmarkKind.ECONOMIC,
                        provider_id=str(provider_attempt.get("provider_id", "unknown")),
                        endpoint_id=str(provider_attempt.get("endpoint_id", "unknown")),
                        route_mode=route_mode,
                        route_id=route_id,
                        connection_state=state,
                        acknowledgement_rtt_ms=_elapsed_ms(
                            submit_started,
                            provider_attempt.get("acknowledged_mono_ns"),
                        ),
                        detection_to_build_ms=_elapsed_ms(
                            detected, telemetry.get("build_started_mono_ns")
                        ),
                        quote_generation_ms=_attribute_float(
                            telemetry, "quote_generation_ms"
                        ),
                        transaction_build_ms=_elapsed_ms(
                            telemetry.get("build_started_mono_ns"),
                            telemetry.get("build_completed_mono_ns"),
                        ),
                        signing_ms=_elapsed_ms(
                            telemetry.get("signing_started_mono_ns"),
                            telemetry.get("signing_completed_mono_ns"),
                        ),
                        detection_to_submit_ms=_elapsed_ms(detected, submit_started),
                        submit_to_processed_ms=(
                            _elapsed_ms(
                                submit_started, telemetry.get("processed_mono_ns")
                            )
                            if accepted
                            else None
                        ),
                        submit_to_confirmed_ms=(
                            _elapsed_ms(
                                submit_started, telemetry.get("confirmed_mono_ns")
                            )
                            if accepted
                            else None
                        ),
                        submit_to_finalized_ms=(
                            _elapsed_ms(
                                submit_started, telemetry.get("finalized_mono_ns")
                            )
                            if accepted
                            else None
                        ),
                        submit_to_landed_ms=(
                            _elapsed_ms(submit_started, processed) if accepted else None
                        ),
                        detection_to_landed_ms=(
                            detection_to_landed if accepted else None
                        ),
                        launch_to_detection_ms=launch_to_detection,
                        launch_to_landed_ms=(
                            launch_to_detection + detection_to_landed
                            if accepted
                            and launch_to_detection is not None
                            and detection_to_landed is not None
                            else None
                        ),
                        launch_slot=_optional_int(telemetry.get("launch_slot")),
                        detection_slot=_optional_int(telemetry.get("detection_slot")),
                        submission_slot=_optional_int(
                            provider_attempt.get("submitted_slot")
                            or telemetry.get("submitted_slot")
                        ),
                        landed_slot=_optional_int(telemetry.get("landed_slot")),
                        success=outcome.success and accepted,
                        ambiguous=(
                            classification_value in ambiguous_values
                            or (
                                accepted
                                and not outcome.success
                                and classification is None
                            )
                        ),
                        error_classification=classification,
                        base_fee_lamports=_optional_int(
                            telemetry.get("base_network_fee_lamports")
                        ),
                        priority_fee_lamports=_optional_int(
                            telemetry.get("priority_fee_lamports")
                        ),
                        jito_tip_lamports=int(telemetry.get("jito_tip_lamports") or 0),
                        rent_lamports=_optional_int(telemetry.get("rent_lamports")),
                        other_known_cost_lamports=int(
                            telemetry.get("other_known_cost_lamports") or 0
                        ),
                        compute_unit_price_micro_lamports=_optional_int(
                            telemetry.get("compute_unit_price_micro_lamports")
                        ),
                        compute_units_consumed=_optional_int(
                            telemetry.get("attributes", {}).get(
                                "compute_units_consumed"
                            )
                            if isinstance(telemetry.get("attributes"), dict)
                            else None
                        ),
                        transaction_size_bytes=_optional_int(
                            telemetry.get("transaction_size_bytes")
                        ),
                        blockhash_age_ms=_optional_float(
                            telemetry.get("blockhash_age_ms_at_submission")
                        ),
                        signature=outcome.signature,
                        logical_trade_id=str(
                            telemetry.get("logical_trade_id")
                            or telemetry.get("execution_id")
                        ),
                        execution_variant=str(
                            telemetry.get("execution_variant", "standard")
                        ),
                    )
                )


class DetectionCorrelator:
    """Persist passive launch observations; it never owns an executor."""

    def __init__(
        self,
        store: BenchmarkStore,
        session_id: str,
        sink: AsyncDetectionSink | None = None,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.sink = sink

    async def observe(self, token: TokenInfo) -> None:
        from execution.detection import detection_for

        timing = detection_for(token)
        now = datetime.now(UTC)
        metadata = token.additional_data or {}
        signature = _first_string(
            metadata,
            "signature",
            "transaction_signature",
            "creation_signature",
            "tx_signature",
        )
        observation = DetectionObservation(
            session_id=self.session_id,
            source=timing.source if timing is not None else "unknown",
            mint=str(token.mint),
            creation_signature=signature,
            observed_at=timing.event_observed_at if timing is not None else now,
            observed_mono_ns=(
                timing.event_observed_mono_ns if timing is not None else monotonic_ns()
            ),
            launch_slot=timing.launch_slot if timing is not None else None,
            detection_slot=timing.event_slot if timing is not None else None,
            transaction_slot=(timing.transaction_slot if timing is not None else None),
            processing_started_mono_ns=(
                timing.hunter_processing_started_mono_ns if timing is not None else None
            ),
            trade_request_mono_ns=(
                timing.trade_request_created_mono_ns if timing is not None else None
            ),
        )
        if self.sink is None:
            self.store.record_detection(observation)
        else:
            self.sink.record_nowait(observation)


class AsyncDetectionSink:
    """Move passive SQLite writes away from listener receive callbacks."""

    def __init__(self, store: BenchmarkStore) -> None:
        self.store = store
        self.queue: asyncio.Queue[DetectionObservation | None] = asyncio.Queue()
        self.worker: asyncio.Task[None] | None = None
        self.failure: Exception | None = None

    async def start(self) -> None:
        if self.worker is None:
            self.worker = asyncio.create_task(
                self._run(), name="hunter-benchmark-detection-writer"
            )

    def record_nowait(self, observation: DetectionObservation) -> None:
        if self.worker is None or self.worker.done():
            raise RuntimeError("benchmark detection sink is not running")
        self.queue.put_nowait(observation)

    async def close(self) -> None:
        if self.worker is None:
            return
        await self.queue.put(None)
        await self.worker
        self.worker = None
        if self.failure is not None:
            raise RuntimeError(
                "benchmark detection persistence failed"
            ) from self.failure

    async def _run(self) -> None:
        while True:
            observation = await self.queue.get()
            try:
                if observation is None:
                    return
                await asyncio.to_thread(self.store.record_detection, observation)
            except Exception as error:  # noqa: BLE001
                self.failure = error
            finally:
                self.queue.task_done()


async def run_transport_probe(
    *,
    store: BenchmarkStore,
    session_id: str,
    provider_id: str,
    endpoint_id: str,
    probe_name: str,
    probe: ReadProbe,
    connection_state: ConnectionState,
) -> BenchmarkAttempt:
    """Measure one non-economic provider read and retain failures."""
    attempt_id = f"{probe_name}:{uuid4()}"
    started = monotonic_ns()
    error_classification = None
    details: dict[str, str | int | float | bool] = {"probe": probe_name}
    success = False
    try:
        await probe()
        success = True
    except ExecutionError as error:
        error_classification = error.classification
        details["error"] = sanitize_text(str(error))
    except aiohttp.ClientResponseError as error:
        if error.status == HTTP_RATE_LIMITED:
            error_classification = ErrorClassification.RPC_RATE_LIMIT
        elif error.status in HTTP_AUTH_FAILURES:
            error_classification = ErrorClassification.PROVIDER_AUTHENTICATION_FAILURE
        else:
            error_classification = ErrorClassification.RPC_REJECTION
        details["error"] = sanitize_text(str(error))
    except (TimeoutError, aiohttp.ClientError) as error:
        error_classification = ErrorClassification.RPC_TRANSPORT_FAILURE
        details["error"] = sanitize_text(str(error) or "timeout")
    except Exception as error:  # noqa: BLE001
        error_classification = ErrorClassification.PROVIDER_UNAVAILABLE
        details["error"] = sanitize_text(str(error))
    elapsed_ms = (monotonic_ns() - started) / 1_000_000
    attempt = BenchmarkAttempt(
        session_id=session_id,
        attempt_id=attempt_id,
        kind=BenchmarkKind.TRANSPORT,
        provider_id=provider_id,
        endpoint_id=endpoint_id,
        route_mode="read",
        route_id=probe_name,
        connection_state=connection_state,
        acknowledgement_rtt_ms=elapsed_ms,
        success=success,
        error_classification=error_classification,
        details=details,
    )
    store.record_attempt(attempt)
    return attempt


def validate_provider_matrix(
    selected: tuple[str, ...], routing: ExecutionRoutingConfig
) -> None:
    """Allow only explicitly configured, enabled submit providers."""
    configured = {
        provider.provider_id for provider in routing.for_role(ProviderRole.SUBMIT)
    }
    unknown = set(selected) - configured
    if unknown:
        raise ValueError(
            f"benchmark providers are not enabled/configured: {sorted(unknown)}"
        )


def _first_string(metadata: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _elapsed_ms(start: object, end: object) -> float | None:
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    return (end - start) / 1_000_000


def _optional_int(value: object) -> int | None:
    return (
        int(value) if isinstance(value, int) and not isinstance(value, bool) else None
    )


def _optional_float(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def _attribute_float(telemetry: dict[str, object], name: str) -> float | None:
    attributes = telemetry.get("attributes")
    return (
        _optional_float(attributes.get(name)) if isinstance(attributes, dict) else None
    )


def _wall_elapsed_ms(start: datetime | None, end: object) -> float | None:
    if start is None or not isinstance(end, str):
        return None
    try:
        observed = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (observed - start).total_seconds() * 1_000


def _connection_state(attempt: dict[str, object]) -> ConnectionState:
    if attempt.get("connection_session_created"):
        generation = attempt.get("connection_session_generation")
        return (
            ConnectionState.RECONNECTED
            if isinstance(generation, int) and generation > 1
            else ConnectionState.COLD
        )
    return ConnectionState.WARM
