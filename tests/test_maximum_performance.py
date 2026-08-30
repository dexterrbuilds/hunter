"""Offline characterization of Hunter's maximum-performance infrastructure."""

from __future__ import annotations

import asyncio
import struct
import sys
import unittest
from pathlib import Path

import yaml
from solders.pubkey import Pubkey
from solders.signature import Signature

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from protocol_fixtures import token_info  # noqa: E402

from config_loader import validate_config  # noqa: E402
from core.pubkeys import quote_token_program  # noqa: E402
from execution.caches import BlockhashCacheRefresher, JitoTipCache  # noqa: E402
from execution.detection import detection_for  # noqa: E402
from execution.ports import (  # noqa: E402
    BlockhashContext,
    ExecutionContext,
    SignedTransaction,
    UnsignedTransaction,
)
from execution.prepared import ExecutionVariantPreparer  # noqa: E402
from execution.providers.adapters import (  # noqa: E402
    HeliusSenderMaxSubmitter,
    JitoTransactionSubmitter,
    TritonJetSubmitter,
)
from execution.providers.capabilities import (  # noqa: E402
    HELIUS_SENDER_MAX_CAPABILITIES,
    JITO_CAPABILITIES,
    STANDARD_CAPABILITIES,
    SWQOS_CAPABILITIES,
)
from execution.providers.config import (  # noqa: E402
    BroadcastMode,
    ProviderEndpoint,
    ProviderKind,
)
from execution.providers.factory import routing_config_from_dict  # noqa: E402
from execution.providers.transport import TransportResponse  # noqa: E402
from execution.routing import SubmissionRouter  # noqa: E402
from execution.telemetry import ExecutionTelemetry  # noqa: E402
from execution.telemetry_sink import AsyncTelemetrySink  # noqa: E402
from interfaces.core import Platform  # noqa: E402
from monitoring.performance.aggregator import EarliestEventAggregator  # noqa: E402
from monitoring.performance.config import (  # noqa: E402
    FeedConfig,
    FeedKind,
    InfrastructureProfile,
    infrastructure_config_from_dict,
)
from monitoring.performance.fast_path import (  # noqa: E402
    FastPathConfidence,
    assess_fast_path,
)
from monitoring.performance.geyser_feeds import (  # noqa: E402
    RabbitStreamListener,
    TritonRiptideListener,
)
from monitoring.performance.models import (  # noqa: E402
    ClaimState,
    CorrelatedLaunch,
    DetectionIdentity,
    DetectionObservation,
)
from monitoring.performance.readiness import (  # noqa: E402
    ComponentState,
    HunterReadiness,
    ReadinessSupervisor,
)
from monitoring.performance.shred_ingress import (  # noqa: E402
    FramedTransactionReconstructor,
    ShredPacket,
)


class FakeTransport:
    """Network-free keepalive transport for provider adapters."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def post_json(self, endpoint, payload, **kwargs):
        self.calls.append((endpoint, payload, kwargs))
        return TransportResponse(200, {"result": "sig"}, {}, connection_reused=True)

    async def warm(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        return True

    async def close(self):
        return None


def execution_context(variant: str = "standard", tip: int = 0):
    return ExecutionContext(
        logical_trade_id="trade",
        execution_id="execution",
        execution_variant=variant,
        blockhash=BlockhashContext.observed("hash", 100),
        signature="sig",
        compute_unit_limit=100_000,
        compute_unit_price_micro_lamports=1,
        jito_tip_lamports=tip,
        metadata={"tip_instruction_count": 1 if tip else 0},
    )


def observation(
    source: str,
    *,
    signature: str | None = None,
    slot: int = 10,
    region: str | None = None,
):
    info = token_info()
    info.quote_token_program_id = quote_token_program(info.quote_mint)
    return DetectionObservation(
        DetectionIdentity(str(info.mint), signature, slot),
        info,
        source,
        region=region,
        slot=slot,
        transaction_signature=signature,
        confidence=FastPathConfidence.AUTHORITATIVE_EVENT_STATE,
    )


class InfrastructureConfigurationTests(unittest.TestCase):
    @staticmethod
    def _bot_config():
        return {
            "name": "test",
            "rpc_endpoint": "https://rpc.invalid",
            "wss_endpoint": "wss://rpc.invalid",
            "private_key": "placeholder",
            "platform": "pump_fun",
            "trade": {
                "buy_amount": 0.001,
                "buy_slippage": 0.1,
                "sell_slippage": 0.1,
            },
            "filters": {"listener_type": "aggregate", "max_token_age": 1.0},
            "risk": {"enforce": True},
            "execution": {
                "enabled": True,
                "providers": [
                    {
                        "id": "rpc",
                        "kind": "standard_rpc",
                        "endpoint": "https://rpc.invalid",
                        "roles": ["submit"],
                    }
                ],
            },
            "infrastructure": {
                "profile": "maximum_performance",
                "feeds": [
                    {
                        "id": "logs",
                        "kind": "logs",
                        "endpoint": "wss://rpc.invalid",
                    }
                ],
            },
        }

    def test_maximum_performance_requires_enabled_feed(self):
        with self.assertRaisesRegex(ValueError, "at least one enabled feed"):
            infrastructure_config_from_dict({"profile": "maximum_performance"})

    def test_regional_profile_and_bounds_are_explicit(self):
        value = infrastructure_config_from_dict(
            {
                "profile": "maximum_performance",
                "region": "amsterdam",
                "maximum_blockhash_age_ms": 2_000,
                "feeds": [
                    {
                        "id": "rabbit-ams",
                        "kind": "rabbitstream",
                        "endpoint": "https://rabbitstream.ams.shyft.to/",
                        "token": "placeholder",
                        "region": "amsterdam",
                        "required": True,
                    }
                ],
            }
        )
        self.assertEqual(value.profile, InfrastructureProfile.MAXIMUM_PERFORMANCE)
        self.assertEqual(value.region, "amsterdam")
        self.assertTrue(value.feeds[0].required)

    def test_paid_feed_requires_explicit_credential(self):
        with self.assertRaisesRegex(ValueError, "credential token"):
            FeedConfig("rabbit", FeedKind.RABBITSTREAM, "https://example.invalid")

    def test_feed_repr_does_not_leak_endpoint_or_token(self):
        feed = FeedConfig(
            "rabbit",
            FeedKind.RABBITSTREAM,
            "https://rabbit.invalid/credential-path",
            token="private-feed-token",  # noqa: S106
        )
        self.assertNotIn("credential-path", repr(feed))
        self.assertNotIn("private-feed-token", repr(feed))

    def test_invalid_commitment_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "commitment"):
            FeedConfig(
                "riptide",
                FeedKind.RIPTIDE,
                "https://example.invalid",
                token="placeholder",  # noqa: S106
                commitment="optimistic",
            )

    def test_sender_regions_and_required_policy_parse(self):
        config = routing_config_from_dict(
            {
                "enabled": True,
                "providers": [
                    {
                        "id": "jet-ams",
                        "kind": "triton_jet",
                        "endpoint": "https://jet.invalid/token",
                        "region": "amsterdam",
                        "required": True,
                    }
                ],
            }
        )
        endpoint = config.providers[0]
        self.assertEqual(endpoint.region, "amsterdam")
        self.assertTrue(endpoint.required)
        self.assertNotIn("token", endpoint.endpoint_id)

    def test_maximum_profile_requires_aggregate_listener(self):
        config = self._bot_config()
        config["filters"]["listener_type"] = "geyser"
        with self.assertRaisesRegex(ValueError, "listener_type: aggregate"):
            validate_config(config)

    def test_maximum_profile_requires_execution_routing(self):
        config = self._bot_config()
        config["execution"]["enabled"] = False
        with self.assertRaisesRegex(ValueError, "execution.enabled"):
            validate_config(config)

    def test_maximum_profile_requires_active_risk_engine(self):
        config = self._bot_config()
        config["risk"]["enforce"] = False
        with self.assertRaisesRegex(ValueError, "risk.enforce"):
            validate_config(config)

    def test_safe_example_is_parseable_and_trading_disabled(self):
        path = ROOT / "config" / "examples" / "hunter-maximum-performance.yaml"
        value = yaml.safe_load(path.read_text())
        self.assertFalse(value["enabled"])
        self.assertFalse(value["risk"]["trading_enabled"])
        self.assertTrue(value["risk"]["emergency_kill_switch"])
        infrastructure_config_from_dict(value["infrastructure"])
        for provider in value["execution"]["providers"]:
            if provider["endpoint"].startswith("${"):
                provider["endpoint"] = "https://provider.invalid/path"
        routing_config_from_dict(value["execution"])


class FastPathAndAggregationTests(unittest.TestCase):
    def test_authoritative_event_state_is_zero_rpc_eligible(self):
        info = token_info()
        info.quote_token_program_id = quote_token_program(info.quote_mint)
        result = assess_fast_path(info)
        self.assertEqual(
            result.confidence, FastPathConfidence.AUTHORITATIVE_EVENT_STATE
        )
        self.assertTrue(result.may_skip_hot_path_reads)

    def test_missing_dynamic_state_requires_refresh(self):
        info = token_info()
        info.creator = None
        result = assess_fast_path(info)
        self.assertEqual(result.confidence, FastPathConfidence.REQUIRES_REFRESH)
        self.assertIn("creator", result.missing_fields)

    def test_letsbonk_is_not_opted_into_pump_fast_path(self):
        info = token_info()
        info.platform = Platform.LETS_BONK
        self.assertEqual(
            assess_fast_path(info).confidence, FastPathConfidence.UNSUPPORTED
        )

    def test_earliest_valid_event_emits_exactly_once(self):
        async def run():
            emitted = []

            async def callback(info):
                emitted.append(info)

            aggregator = EarliestEventAggregator(callback)
            await aggregator.start()
            first = observation("rabbitstream")
            later = observation("triton_riptide", signature="sig")
            aggregator.submit_nowait(first)
            aggregator.submit_nowait(later)
            await aggregator.flush()
            launch = aggregator.launches[first.identity.claim_key]
            await aggregator.close()
            return emitted, launch

        emitted, launch = asyncio.run(run())
        self.assertEqual(len(emitted), 1)
        self.assertEqual(launch.claimed_source, "rabbitstream")
        self.assertEqual(launch.state, ClaimState.TRADE_REQUEST_CREATED)
        self.assertEqual(len(launch.observations), 2)
        self.assertEqual(launch.identity.creation_signature, "sig")

    def test_simultaneous_sources_cannot_double_claim(self):
        async def run():
            emitted = []

            async def callback(info):
                emitted.append(info)

            aggregator = EarliestEventAggregator(callback)
            await aggregator.start()
            await asyncio.gather(
                aggregator.submit(observation("rabbitstream")),
                aggregator.submit(observation("triton_shreds")),
            )
            await aggregator.flush()
            await aggregator.close()
            return emitted

        self.assertEqual(len(asyncio.run(run())), 1)

    def test_claim_preserves_source_identity_and_pipeline_timing(self):
        async def run():
            emitted = []

            async def callback(info):
                emitted.append(info)

            aggregator = EarliestEventAggregator(callback)
            await aggregator.start()
            item = observation(
                "rabbitstream",
                signature=str(Signature.default()),
                region="amsterdam",
            )
            item.parser_completed_mono_ns = item.received_mono_ns + 10
            item.validation_completed_mono_ns = item.received_mono_ns + 20
            aggregator.submit_nowait(item)
            await aggregator.flush()
            await aggregator.close()
            return detection_for(emitted[0])

        telemetry = asyncio.run(run())
        self.assertIsNotNone(telemetry)
        self.assertEqual(telemetry.source_region, "amsterdam")
        self.assertEqual(telemetry.transaction_signature, str(Signature.default()))
        self.assertIsNotNone(telemetry.correlation_completed_mono_ns)
        self.assertIsNotNone(telemetry.claim_completed_mono_ns)

    def test_replayed_event_remains_telemetry_only(self):
        async def run():
            emitted = []

            async def callback(info):
                emitted.append(info)

            aggregator = EarliestEventAggregator(callback)
            await aggregator.start()
            item = observation("rabbitstream", signature="sig")
            for _ in range(3):
                aggregator.submit_nowait(item)
            await aggregator.flush()
            await aggregator.close()
            return emitted, aggregator.launches[item.identity.claim_key]

        emitted, launch = asyncio.run(run())
        self.assertEqual(len(emitted), 1)
        self.assertEqual(len(launch.observations), 3)

    def test_bounded_queue_drops_instead_of_blocking_ingress(self):
        async def callback(info):
            del info

        aggregator = EarliestEventAggregator(callback, queue_size=1)
        self.assertTrue(aggregator.submit_nowait(observation("a")))
        self.assertFalse(aggregator.submit_nowait(observation("b")))
        self.assertEqual(aggregator.dropped_observations, 1)

    def test_cross_feed_relative_arrival_is_measured(self):
        first = observation("rabbitstream")
        second = observation("riptide")
        second.received_mono_ns = first.received_mono_ns + 2_500_000
        launch = CorrelatedLaunch(first.identity, [first, second])
        self.assertEqual(launch.relative_arrival_ms()["riptide"], 2.5)


class ShredAndGeyserAdapterTests(unittest.TestCase):
    def test_sidecar_frame_reconstructs_exact_transaction_once(self):
        signature = Signature.default()
        wire = b"signed-transaction-wire"
        payload = (
            struct.pack("<4sQ64sI", b"HNTR", 123, bytes(signature), len(wire)) + wire
        )
        result = FramedTransactionReconstructor().feed(
            ShredPacket(payload, 100, ("127.0.0.1", 9000))
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].slot, 123)
        self.assertEqual(result[0].signature, str(signature))
        self.assertEqual(result[0].wire_bytes, wire)

    def test_malformed_or_partial_frame_is_not_guessed(self):
        decoder = FramedTransactionReconstructor()
        self.assertEqual(decoder.feed(ShredPacket(b"raw-shred", 1)), [])
        invalid = struct.pack("<4sQ64sI", b"HNTR", 1, bytes(64), 99) + b"short"
        self.assertEqual(decoder.feed(ShredPacket(invalid, 1)), [])

    def test_rabbitstream_uses_generic_processed_yellowstone_path(self):
        listener = RabbitStreamListener(
            "rabbitstream.ams.shyft.to:443",
            "placeholder",
            region="amsterdam",
            platforms=[],
        )
        self.assertEqual(listener.source_name, "rabbitstream")
        self.assertEqual(listener.source_region, "amsterdam")
        self.assertEqual(listener.commitment, "processed")

    def test_riptide_remains_generic_and_region_configurable(self):
        listener = TritonRiptideListener(
            "riptide.invalid:443",
            "placeholder",
            region="tokyo",
            platforms=[],
            commitment="confirmed",
        )
        self.assertEqual(listener.source_name, "triton_riptide")
        self.assertEqual(listener.source_region, "tokyo")
        self.assertEqual(listener.commitment, "confirmed")


class CachePreparationAndReadinessTests(unittest.TestCase):
    def test_blockhash_cache_rejects_stale_and_expired_values(self):
        class Provider:
            async def get_blockhash(self):
                return BlockhashContext.observed("hash", 20, observed_slot=10)

        async def run():
            cache = BlockhashCacheRefresher(Provider(), maximum_age_ms=10)
            value = await cache.refresh()
            return cache, value

        cache, value = asyncio.run(run())
        self.assertIsNotNone(cache.current())
        self.assertIsNone(cache.current(current_block_height=21))
        self.assertIsNone(cache.current(now_mono=value.fetched_mono + 0.011))

    def test_blockhash_refresher_runs_in_background_and_stops(self):
        class Provider:
            calls = 0

            async def get_blockhash(self):
                self.calls += 1
                return BlockhashContext.observed(f"hash-{self.calls}", 100)

        async def run():
            provider = Provider()
            cache = BlockhashCacheRefresher(
                provider, refresh_interval_seconds=0.001, maximum_age_ms=100
            )
            await cache.start()
            await asyncio.sleep(0.004)
            await cache.close()
            return provider.calls, cache.current()

        calls, current = asyncio.run(run())
        self.assertGreaterEqual(calls, 2)
        self.assertIsNotNone(current)

    def test_jito_tip_cache_clamps_estimate_to_operator_cap(self):
        async def estimator():
            return 50_000

        async def run():
            cache = JitoTipCache(
                estimator,
                source="provider",
                strategy="p75",
                minimum_lamports=1_000,
                maximum_lamports=10_000,
            )
            value = await cache.refresh()
            return value, cache.selection()

        value, current = asyncio.run(run())
        self.assertEqual(value.estimated_lamports, 50_000)
        self.assertEqual(value.selected_lamports, 10_000)
        self.assertEqual(current.strategy, "p75")

    def test_variant_is_signed_and_serialized_once(self):
        class Signer:
            public_key = Pubkey.default()
            calls = 0

            async def sign(self, transaction):
                self.calls += 1
                return SignedTransaction(transaction.message_bytes + b"-signed", "sig")

        async def run():
            signer = Signer()
            preparer = ExecutionVariantPreparer(signer)
            unsigned = UnsignedTransaction(b"message", 7, (Pubkey.default(),))
            one = await preparer.prepare("standard", unsigned)
            two = await preparer.prepare("standard", unsigned)
            return signer.calls, one, two

        calls, one, two = asyncio.run(run())
        self.assertEqual(calls, 1)
        self.assertIs(one, two)

    def test_readiness_fails_closed_for_required_component(self):
        readiness = ReadinessSupervisor(allow_degraded=False)
        readiness.register("blockhash", required=True)
        readiness.update("blockhash", ComponentState.DEGRADED, "stale")
        self.assertEqual(readiness.state, HunterReadiness.NOT_READY)
        self.assertFalse(readiness.may_trade)

    def test_degraded_operation_requires_explicit_permission(self):
        readiness = ReadinessSupervisor(allow_degraded=True)
        readiness.register("optional-feed", required=False)
        readiness.update("optional-feed", ComponentState.DEGRADED, "reconnecting")
        self.assertEqual(readiness.state, HunterReadiness.DEGRADED)
        self.assertTrue(readiness.may_trade)

    def test_concurrent_readiness_initialization_records_failures(self):
        async def good():
            return True

        async def bad():
            raise OSError

        async def run():
            readiness = ReadinessSupervisor(allow_degraded=False)
            readiness.register("signer", required=True)
            readiness.register("optional", required=False)
            state = await readiness.initialize({"signer": good, "optional": bad})
            return readiness, state

        readiness, state = asyncio.run(run())
        self.assertEqual(state, HunterReadiness.DEGRADED)
        report = readiness.report()
        self.assertIn("OSError", str(report))
        self.assertNotIn("credential must not appear", str(report))


class ProviderAndPersistenceTests(unittest.TestCase):
    def test_provider_capabilities_allow_only_compatible_races(self):
        self.assertTrue(
            STANDARD_CAPABILITIES.race_compatible_with(
                SWQOS_CAPABILITIES, variant="standard"
            )
        )
        self.assertFalse(
            HELIUS_SENDER_MAX_CAPABILITIES.race_compatible_with(
                JITO_CAPABILITIES, variant="sender_max_tipped"
            )
        )

    def test_helius_sender_max_enforces_tip_and_priority_fee(self):
        transport = FakeTransport()
        endpoint = ProviderEndpoint(
            "helius-max",
            ProviderKind.HELIUS_SENDER_MAX,
            "http://ams-sender.helius-rpc.com/fast?api-key=placeholder",
            minimum_tip_lamports=1_000,
            maximum_tip_lamports=10_000,
            region="amsterdam",
        )
        result = asyncio.run(
            HeliusSenderMaxSubmitter(endpoint, transport).submit(
                SignedTransaction(b"wire", "sig"),
                execution_context("sender_max_tipped", 1_000),
            )
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.provider_region, "amsterdam")
        self.assertEqual(len(transport.calls), 1)

    def test_sender_max_rejects_standard_variant_before_network(self):
        transport = FakeTransport()
        endpoint = ProviderEndpoint(
            "helius-max",
            ProviderKind.HELIUS_SENDER_MAX,
            "https://sender.invalid/fast",
            minimum_tip_lamports=1,
            maximum_tip_lamports=10_000,
        )
        result = asyncio.run(
            HeliusSenderMaxSubmitter(endpoint, transport).submit(
                SignedTransaction(b"wire", "sig"), execution_context()
            )
        )
        self.assertFalse(result.accepted)
        self.assertEqual(transport.calls, [])

    def test_triton_jet_reuses_standard_signed_bytes(self):
        transport = FakeTransport()
        endpoint = ProviderEndpoint(
            "jet",
            ProviderKind.TRITON_JET,
            "https://cascade.invalid/token",
        )
        result = asyncio.run(
            TritonJetSubmitter(endpoint, transport).submit(
                SignedTransaction(b"same-wire", "sig"), execution_context()
            )
        )
        self.assertTrue(result.accepted)
        self.assertEqual(transport.calls[0][1]["params"][0], "c2FtZS13aXJl")

    def test_router_rejects_incompatible_tipped_variants(self):
        helius_endpoint = ProviderEndpoint(
            "helius-max",
            ProviderKind.HELIUS_SENDER_MAX,
            "https://sender.invalid/fast",
            minimum_tip_lamports=1,
            maximum_tip_lamports=10_000,
        )
        jito_endpoint = ProviderEndpoint(
            "jito",
            ProviderKind.JITO,
            "https://jito.invalid/api/v1/transactions",
        )
        router = SubmissionRouter(
            [
                HeliusSenderMaxSubmitter(helius_endpoint, FakeTransport()),
                JitoTransactionSubmitter(jito_endpoint, FakeTransport()),
            ],
            mode=BroadcastMode.RACE,
        )
        with self.assertRaisesRegex(ValueError, "does not accept"):
            asyncio.run(
                router.submit(
                    SignedTransaction(b"wire", "sig"),
                    execution_context("sender_max_tipped", 1_000),
                )
            )

    def test_single_mode_ignores_inactive_incompatible_secondary(self):
        primary_transport = FakeTransport()
        standard = ProviderEndpoint(
            "rpc",
            ProviderKind.STANDARD_RPC,
            "https://rpc.invalid",
            priority=1,
        )
        sender_max = ProviderEndpoint(
            "helius-max",
            ProviderKind.HELIUS_SENDER_MAX,
            "https://sender.invalid/fast",
            priority=2,
            minimum_tip_lamports=1,
            maximum_tip_lamports=10_000,
        )
        router = SubmissionRouter(
            [
                TritonJetSubmitter(
                    ProviderEndpoint(
                        "jet",
                        ProviderKind.TRITON_JET,
                        standard.endpoint,
                        priority=standard.priority,
                    ),
                    primary_transport,
                ),
                HeliusSenderMaxSubmitter(sender_max, FakeTransport()),
            ],
            mode=BroadcastMode.SINGLE,
        )
        result = asyncio.run(
            router.submit(
                SignedTransaction(b"wire", "sig"),
                execution_context("standard"),
            )
        )
        self.assertTrue(result.accepted)
        self.assertEqual(len(primary_transport.calls), 1)

    def test_async_telemetry_queue_is_capped_and_non_blocking(self):
        class Store:
            def save_telemetry(self, telemetry, attempt):
                del telemetry, attempt

        async def run():
            sink = AsyncTelemetrySink(Store(), maximum_queue_size=1)
            sink._worker = asyncio.create_task(asyncio.sleep(60))
            first = sink.record_nowait(ExecutionTelemetry("one"), 1)
            second = sink.record_nowait(ExecutionTelemetry("two"), 1)
            sink._worker.cancel()
            await asyncio.gather(sink._worker, return_exceptions=True)
            return first, second, sink.dropped_records

        first, second, dropped = asyncio.run(run())
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(dropped, 1)

    def test_endpoint_repr_and_identifier_do_not_leak_token(self):
        endpoint = ProviderEndpoint(
            "jet",
            ProviderKind.TRITON_JET,
            "https://cascade.invalid/very-secret-token",
            headers={"x-token": "also-secret"},
        )
        self.assertNotIn("very-secret-token", repr(endpoint))
        self.assertNotIn("also-secret", repr(endpoint))
        self.assertNotIn("very-secret-token", endpoint.endpoint_id)


if __name__ == "__main__":
    unittest.main()
