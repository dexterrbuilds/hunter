"""Isolated commands for passive, transport, and live economic benchmarks."""

# CLI validation errors are deliberately direct and environment resolution is
# limited to the selected benchmark sections.
# ruff: noqa: PLC0415, PLR0913, PLR0917, TRY003, TRY004

from __future__ import annotations

import argparse
import asyncio
import json
import os
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from time import monotonic_ns
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiohttp
import yaml

from benchmark.live_config import (
    LiveBenchmarkConfig,
    live_benchmark_config_from_dict,
)
from benchmark.live_models import BenchmarkKind, ConnectionState
from benchmark.live_report import build_live_report, export_report, render_text_report
from benchmark.live_session import (
    AsyncDetectionSink,
    DetectionCorrelator,
    LiveBenchmarkSession,
    run_transport_probe,
    validate_provider_matrix,
)
from benchmark.live_store import BenchmarkStore
from domain.amounts import BasisPoints, maximum_after_slippage
from execution.errors import ErrorClassification, ExecutionError
from execution.providers.config import ProviderEndpoint, ProviderKind, ProviderRole
from execution.providers.factory import routing_config_from_dict
from interfaces.core import Platform
from monitoring.listener_factory import ListenerFactory
from utils.redaction import register_config_secrets

if TYPE_CHECKING:
    from monitoring.base_listener import BaseTokenListener


def live_main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one explicitly authorized tiny economic benchmark trial"
    )
    parser.add_argument("config", help="normal Hunter bot YAML with benchmark section")
    parser.add_argument("--route", required=True, help="one configured matrix route ID")
    parser.add_argument("--database", default="data/hunter-benchmarks.sqlite3")
    parser.add_argument("--allow-live", action="store_true")
    args = parser.parse_args()
    asyncio.run(_run_live(args))


def detection_main() -> None:
    parser = argparse.ArgumentParser(
        description="Observe launch detection sources without trading"
    )
    parser.add_argument("config", help="benchmark observer YAML")
    parser.add_argument("--database", default="data/hunter-benchmarks.sqlite3")
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--region-label", default="unspecified")
    args = parser.parse_args()
    asyncio.run(_run_detection(args))


def transport_main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure configured standard RPC reads without transactions"
    )
    parser.add_argument("config", help="YAML containing execution providers")
    parser.add_argument("--database", default="data/hunter-benchmarks.sqlite3")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--region-label", default="unspecified")
    parser.add_argument(
        "--warmup",
        action="store_true",
        help="record a warm-up request before measured probes",
    )
    args = parser.parse_args()
    asyncio.run(_run_transport(args))


def report_main() -> None:
    parser = argparse.ArgumentParser(description="Report controlled benchmark results")
    parser.add_argument("database")
    parser.add_argument("--session")
    parser.add_argument("--minimum-samples", type=int, default=20)
    parser.add_argument("--export")
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    args = parser.parse_args()
    store = BenchmarkStore(args.database)
    try:
        report = build_live_report(
            store,
            session_id=args.session,
            minimum_ranking_samples=args.minimum_samples,
        )
        if args.export:
            export_report(report, args.export, args.format)
        print(render_text_report(report))
    finally:
        store.close()


async def _run_live(args: argparse.Namespace) -> None:
    from benchmark.economic import HunterEconomicExecutor
    from config_loader import load_bot_config

    bot_config = load_bot_config(args.config)
    benchmark = live_benchmark_config_from_dict(bot_config.get("benchmark", {}))
    routing = routing_config_from_dict(bot_config.get("execution"))
    route = next(
        (item for item in benchmark.provider_matrix if item.route_id == args.route),
        None,
    )
    if route is None:
        raise ValueError(f"unknown configured benchmark route: {args.route}")
    validate_provider_matrix(route.providers, routing)
    benchmark.authorize(
        cli_allow_live=args.allow_live,
        risk_enforced=bool(bot_config.get("risk", {}).get("enforce", False)),
    )
    _validate_live_fee_caps(bot_config, benchmark)
    print(json.dumps(benchmark.maximum_exposure_summary(), indent=2, sort_keys=True))
    if not benchmark.dedicated_wallet:
        print(
            "WARNING: benchmark.dedicated_wallet is false; use a separate "
            "low-balance benchmark wallet."
        )
    store = BenchmarkStore(args.database)
    executor = HunterEconomicExecutor(bot_config, benchmark)
    try:
        session = LiveBenchmarkSession(
            benchmark,
            store,
            cli_allow_live=args.allow_live,
            risk_enforced=True,
        )
        result = await session.execute(executor, route_id=args.route)
        store.complete_session(session.session_id)
        print(
            json.dumps(
                {
                    "session_id": session.session_id,
                    "buy_success": result.buy.success,
                    "buy_signature": result.buy.signature,
                    "exit_policy": benchmark.exit_policy.value,
                    "exit_success": result.exit.success if result.exit else None,
                    "exit_signature": result.exit.signature if result.exit else None,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        await executor.close()
        store.close()


async def _run_detection(args: argparse.Namespace) -> None:
    if args.duration <= 0:
        raise ValueError("duration must be positive")
    raw = _load_selected_yaml(args.config)
    sources = raw.get("detection", {}).get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("detection.sources must be a non-empty list")
    store = BenchmarkStore(args.database)
    session_id = f"detection-{uuid4()}"
    store.create_session(
        session_id,
        BenchmarkKind.DETECTION,
        region_label=args.region_label,
    )
    sink = AsyncDetectionSink(store)
    await sink.start()
    correlator = DetectionCorrelator(store, session_id, sink)
    listeners = [_listener_from_source(_resolve_env(item)) for item in sources]
    tasks = [
        asyncio.create_task(listener.listen_for_tokens(correlator.observe))
        for listener in listeners
    ]
    try:
        await asyncio.sleep(args.duration)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await sink.close()
        store.complete_session(session_id)
        report = build_live_report(store, session_id=session_id)
        print(render_text_report(report))
        store.close()


async def _run_transport(args: argparse.Namespace) -> None:
    if args.iterations <= 0:
        raise ValueError("iterations must be positive")
    raw = _load_selected_yaml(args.config)
    execution = _resolve_env(raw.get("execution", {}))
    transport = _resolve_env(raw.get("transport_benchmark", {}))
    if not isinstance(transport, dict):
        raise TypeError("transport_benchmark must be a mapping")
    probe_specs = _transport_probe_specs(transport)
    routing = routing_config_from_dict(execution)
    providers = {
        item.provider_id: item
        for role in (
            ProviderRole.ACCOUNT_READ,
            ProviderRole.BLOCKHASH,
            ProviderRole.CONFIRM,
        )
        for item in routing.for_role(role)
        if item.kind == ProviderKind.STANDARD_RPC
    }
    if not providers:
        raise ValueError("no enabled standard RPC read providers are configured")
    store = BenchmarkStore(args.database)
    session_id = f"transport-{uuid4()}"
    store.create_session(
        session_id,
        BenchmarkKind.TRANSPORT,
        region_label=args.region_label,
    )
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as client:
        for provider in providers.values():
            request_count = 0
            if args.warmup:
                await _record_rpc_probe(
                    client,
                    store,
                    session_id,
                    provider,
                    "warmup:getHealth",
                    "getHealth",
                    [],
                    ConnectionState.COLD,
                )
                request_count += 1
            for _index in range(args.iterations):
                for probe_name, method, parameters in probe_specs:
                    state = (
                        ConnectionState.COLD
                        if request_count == 0
                        else ConnectionState.WARM
                    )
                    await _record_rpc_probe(
                        client,
                        store,
                        session_id,
                        provider,
                        probe_name,
                        method,
                        parameters,
                        state,
                    )
                    request_count += 1
    store.complete_session(session_id)
    print(render_text_report(build_live_report(store, session_id=session_id)))
    store.close()


def _listener_from_source(value: dict[str, Any]) -> BaseTokenListener:
    listener_type = str(value.get("type", ""))
    return ListenerFactory.create_listener(
        listener_type=listener_type,
        wss_endpoint=value.get("wss_endpoint"),
        geyser_endpoint=value.get("geyser_endpoint"),
        geyser_api_token=value.get("geyser_api_token"),
        geyser_auth_type=value.get("geyser_auth_type", "x-token"),
        pumpportal_url=value.get("pumpportal_url", "wss://pumpportal.fun/api/data"),
        platforms=[Platform.PUMP_FUN],
    )


async def _record_rpc_probe(
    client: aiohttp.ClientSession,
    store: BenchmarkStore,
    session_id: str,
    provider: ProviderEndpoint,
    probe_name: str,
    method: str,
    parameters: list[object],
    state: ConnectionState,
) -> None:
    async def probe() -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": monotonic_ns(),
            "method": method,
            "params": parameters,
        }
        async with client.post(
            provider.endpoint, json=payload, headers=provider.headers
        ) as response:
            response.raise_for_status()
            body = await response.json()
            if "error" in body:
                raise ExecutionError(
                    ErrorClassification.RPC_REJECTION,
                    f"RPC returned an error for {method}",
                    code=(
                        body["error"].get("code")
                        if isinstance(body["error"], dict)
                        else None
                    ),
                )

    await run_transport_probe(
        store=store,
        session_id=session_id,
        provider_id=provider.provider_id,
        endpoint_id=provider.endpoint_id,
        probe_name=probe_name,
        probe=probe,
        connection_state=state,
    )


def _transport_probe_specs(
    value: dict[str, object],
) -> list[tuple[str, str, list[object]]]:
    requested = value.get("probes", ["health", "blockhash"])
    if not isinstance(requested, list) or not all(
        isinstance(item, str) for item in requested
    ):
        raise ValueError("transport_benchmark.probes must be a list of names")
    allowed = {"health", "blockhash", "account", "status", "priority_fee"}
    unknown = set(requested) - allowed
    if unknown:
        raise ValueError(f"unsupported transport probe: {sorted(unknown)[0]}")
    specs: list[tuple[str, str, list[object]]] = []
    for name in requested:
        if name == "health":
            specs.append((name, "getHealth", []))
        elif name == "blockhash":
            specs.append((name, "getLatestBlockhash", [{"commitment": "processed"}]))
        elif name == "account":
            address = _probe_string(value, "account_address")
            specs.append(
                (
                    name,
                    "getAccountInfo",
                    [address, {"encoding": "base64", "commitment": "processed"}],
                )
            )
        elif name == "status":
            signature = _probe_string(value, "status_signature")
            specs.append(
                (
                    name,
                    "getSignatureStatuses",
                    [[signature], {"searchTransactionHistory": True}],
                )
            )
        else:
            accounts = value.get("priority_fee_accounts", [])
            if not isinstance(accounts, list) or not all(
                isinstance(account, str) for account in accounts
            ):
                raise ValueError(
                    "transport_benchmark.priority_fee_accounts must be a list"
                )
            specs.append((name, "getRecentPrioritizationFees", [accounts]))
    return specs


def _probe_string(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"transport_benchmark.{key} is required for this probe")
    return item


def _validate_live_fee_caps(
    bot_config: dict[str, Any], benchmark: LiveBenchmarkConfig
) -> None:
    priority = bot_config.get("priority_fees", {})
    hard_cap_micro_lamports = int(priority.get("hard_cap", 0))
    buy_compute_limit = int(bot_config.get("compute_units", {}).get("buy", 100_000))
    maximum_priority_lamports = (
        hard_cap_micro_lamports * buy_compute_limit + 999_999
    ) // 1_000_000
    if maximum_priority_lamports > benchmark.caps.maximum_priority_fee_lamports:
        raise ValueError("configured priority-fee exposure exceeds benchmark cap")
    slippage = bot_config["trade"]["buy_slippage"]
    slippage_bps = BasisPoints(
        int(
            (Decimal(str(slippage)) * Decimal(10_000)).to_integral_value(
                rounding=ROUND_DOWN
            )
        )
    )
    benchmark.validate_encoded_input(
        maximum_after_slippage(benchmark.quote_amount_raw, slippage_bps)
    )


def _load_selected_yaml(path: str) -> dict[str, Any]:
    with Path(path).open() as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ValueError("benchmark configuration must be a mapping")
    return value


def _resolve_env(value: object) -> object:
    if isinstance(value, dict):
        resolved = {key: _resolve_env(item) for key, item in value.items()}
        register_config_secrets(resolved)
        return resolved
    if isinstance(value, list):
        return [_resolve_env(item) for item in value]
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        name = value[2:-1]
        resolved = os.getenv(name)
        if resolved is None:
            raise ValueError(f"environment variable {name!r} is not set")
        return resolved
    return value
