# Offline orchestration fixtures intentionally mirror positional protocol APIs.
# ruff: noqa: FBT002, FBT003, PLR0913

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from solders.instruction import Instruction
from solders.pubkey import Pubkey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from application.milestone37_config import (  # noqa: E402
    validate_token_launch_config,
    validate_wallet_fleet_config,
    wallet_tracking_config_from_dict,
)
from application.token_launch import (  # noqa: E402
    LaunchCostEstimate,
    LaunchRiskLimits,
    LaunchRiskService,
    PreparedLaunchComponent,
    PumpFunLaunchComponentPreparer,
    TokenLaunchRecoveryService,
    TokenLaunchService,
)
from application.universal_execution import UniversalFastExecution  # noqa: E402
from application.wallet_fleet import (  # noqa: E402
    FleetExitEvaluator,
    FleetValuation,
    WalletFleetExitService,
    WalletFleetService,
)
from application.wallet_tracking import (  # noqa: E402
    AsyncWalletEventStore,
    TrackedWalletService,
)
from core.pubkeys import WSOL_MINT  # noqa: E402
from domain.amounts import BasisPoints, QuoteAmountRaw  # noqa: E402
from domain.intents import (  # noqa: E402
    ExecutionUrgency,
    TradeAction,
    TradeIntent,
    TradeIntentSource,
    default_urgency,
)
from domain.launch import (  # noqa: E402
    FleetExecutionPolicy,
    FleetExitPolicy,
    FleetExitType,
    FleetWallet,
    FleetWalletRole,
    LaunchBuy,
    LaunchExecutionPlan,
    LaunchState,
    SignerReference,
    TokenLaunchRequest,
)
from domain.wallet_tracking import (  # noqa: E402
    CopySizingMode,
    DuplicatePolicy,
    TrackedWallet,
    TrackedWalletAction,
    WalletActivity,
    WalletActivityType,
    WalletTrackingConfig,
)
from execution.bundles import (  # noqa: E402
    BundleObservationState,
    JitoBundleSubmitter,
)
from execution.errors import ErrorClassification, ExecutionError  # noqa: E402
from execution.ports import (  # noqa: E402
    BlockhashContext,
    ExecutionContext,
    SignedTransaction,
    SubmissionResult,
)
from execution.providers.config import (  # noqa: E402
    ProviderEndpoint,
    ProviderKind,
)
from execution.providers.transport import TransportResponse  # noqa: E402
from monitoring.tracked_wallets import (  # noqa: E402
    PumpFunWalletActivityDecoder,
    TrackedWalletProcessor,
    WalletTransactionObservation,
)
from platforms.pumpfun.address_provider import PumpFunAddresses  # noqa: E402
from platforms.pumpfun.launch_builder import (  # noqa: E402
    PumpFunLaunchInstructionBuilder,
)
from storage.sqlite import SCHEMA_VERSION, SQLitePositionStore  # noqa: E402
from utils.idl_parser import IDLParser  # noqa: E402

MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
WALLET = Pubkey.from_string("11111111111111111111111111111111")
OTHER = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
USDC = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")


def buy_intent(source=TradeIntentSource.MANUAL_BUY):
    return TradeIntent(
        action=TradeAction.BUY,
        source=source,
        mint=MINT,
        wallet_id="primary",
        quote_mint=WSOL_MINT,
        quote_amount=QuoteAmountRaw(1_000, WSOL_MINT, 9),
        token_decimals=6,
        slippage=BasisPoints(100),
        urgency=default_urgency(source),
        intent_id=f"intent:{source.value}",
    )


class IntentTests(unittest.IsolatedAsyncioTestCase):
    def test_buy_requires_explicit_amount_and_decimals(self):
        with self.assertRaisesRegex(ValueError, "quote_amount"):
            TradeIntent(
                action=TradeAction.BUY,
                source=TradeIntentSource.MANUAL_BUY,
                mint=MINT,
                wallet_id="primary",
                slippage=BasisPoints(1),
            )

    def test_urgency_does_not_select_provider(self):
        self.assertEqual(
            default_urgency(TradeIntentSource.STOP_LOSS), ExecutionUrgency.CRITICAL
        )
        self.assertEqual(
            default_urgency(TradeIntentSource.TRACKED_WALLET_BUY),
            ExecutionUrgency.HIGH,
        )

    def test_benchmark_categories_are_stable_and_source_specific(self):
        self.assertEqual(
            TradeIntentSource.LAUNCH_SNIPE.benchmark_category, "LAUNCH_SNIPE"
        )
        self.assertEqual(TradeIntentSource.MANUAL_SELL.benchmark_category, "MANUAL")
        self.assertEqual(TradeIntentSource.TAKE_PROFIT.benchmark_category, "TP")
        self.assertEqual(
            TradeIntentSource.WALLET_FLEET_EXIT.benchmark_category, "FLEET_EXIT"
        )

    async def test_all_buy_sources_reach_same_executor(self):
        seen = []

        async def execute(request):
            seen.append(request)
            return "buy"

        async def sell(_request):
            return "sell"

        facade = UniversalFastExecution(execute, sell)
        for source in (
            TradeIntentSource.LAUNCH_SNIPE,
            TradeIntentSource.TRACKED_WALLET_CREATE,
            TradeIntentSource.TRACKED_WALLET_BUY,
            TradeIntentSource.YOLO,
            TradeIntentSource.MANUAL_BUY,
        ):
            await facade.execute(buy_intent(source))
        self.assertEqual(len(seen), 5)
        self.assertEqual(
            {item.intent.source for item in seen}, {item.intent.source for item in seen}
        )


class WalletConfigTests(unittest.TestCase):
    def test_tracking_is_disabled_by_default(self):
        self.assertFalse(wallet_tracking_config_from_dict(None).enabled)

    def test_fixed_amount_is_converted_exactly(self):
        config = wallet_tracking_config_from_dict(
            {
                "enabled": True,
                "wallets": [
                    {
                        "address": str(WALLET),
                        "watch_create": True,
                        "watch_buy": False,
                        "create_action": {
                            "enabled": True,
                            "buy_amount_sol": "0.001",
                        },
                    }
                ],
            }
        )
        self.assertEqual(
            config.wallets[0].create_action.fixed_quote_amount.value, 1_000_000
        )

    def test_duplicate_addresses_fail_closed(self):
        item = {"address": str(WALLET)}
        with self.assertRaisesRegex(ValueError, "unique"):
            wallet_tracking_config_from_dict(
                {"enabled": True, "wallets": [item, dict(item)]}
            )

    def test_fleet_requires_env_signer_references(self):
        with self.assertRaisesRegex(ValueError, "env:"):
            validate_wallet_fleet_config(
                {
                    "enabled": True,
                    "wallets": [{"id": "creator", "signer": "plaintext-key"}],
                    "launch": {"risk_enforced": True},
                }
            )

    def test_enabled_launch_requires_enabled_fleet(self):
        with self.assertRaisesRegex(ValueError, "wallet_fleet.enabled"):
            validate_token_launch_config(
                {"enabled": True, "execution": {"mode": "bundle"}},
                wallet_fleet_enabled=False,
            )

    def test_reference_configuration_is_disabled_and_contains_no_key(self):
        example = yaml.safe_load(
            (ROOT / "config/examples/hunter-wallet-orchestration.yaml").read_text()
        )
        self.assertFalse(example["wallet_tracking"]["enabled"])
        self.assertFalse(example["wallet_fleet"]["enabled"])
        self.assertFalse(example["token_launch"]["enabled"])
        self.assertTrue(
            all(
                item["signer"].startswith("env:")
                for item in example["wallet_fleet"]["wallets"]
            )
        )


class PositionLookup:
    def __init__(self, exists=False):
        self.exists = exists

    def has_open_position(self, _wallet, _mint):
        return self.exists


class WalletTrackingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = SQLitePositionStore(":memory:")
        self.async_store = AsyncWalletEventStore(self.store)
        self.executed = []
        action = TrackedWalletAction(
            True,
            fixed_quote_amount=QuoteAmountRaw(1_000, WSOL_MINT, 9),
        )
        self.wallet = TrackedWallet(WALLET, True, True, action, action, "dev")

    async def asyncTearDown(self):
        self.store.close()

    async def _execute(self, intent):
        self.executed.append(intent)

    def _service(
        self, *, exists=False, policy=DuplicatePolicy.IGNORE_EXISTING_POSITION
    ):
        return TrackedWalletService(
            WalletTrackingConfig(True, policy, (self.wallet,)),
            self.async_store,
            PositionLookup(exists),
            self._execute,
        )

    async def test_create_and_buy_are_independent_events(self):
        service = self._service()
        for kind, suffix in (
            (WalletActivityType.CREATE, "a"),
            (WalletActivityType.BUY, "b"),
        ):
            activity = WalletActivity(
                kind,
                WALLET,
                MINT,
                f"signature-{suffix}",
                10,
                PumpFunAddresses.PROGRAM,
                quote_mint=WSOL_MINT,
                source_quote_amount=QuoteAmountRaw(10_000, WSOL_MINT, 9),
                token_decimals=6,
            )
            await service.handle(activity)
        self.assertEqual(
            [item.source for item in self.executed],
            [
                TradeIntentSource.TRACKED_WALLET_CREATE,
                TradeIntentSource.TRACKED_WALLET_BUY,
            ],
        )

    async def test_same_source_event_is_idempotent(self):
        service = self._service()
        activity = WalletActivity(
            WalletActivityType.BUY,
            WALLET,
            MINT,
            "same-signature",
            10,
            PumpFunAddresses.PROGRAM,
            quote_mint=WSOL_MINT,
            token_decimals=6,
        )
        await service.handle(activity)
        await service.handle(activity)
        self.assertEqual(len(self.executed), 1)

    async def test_default_policy_ignores_existing_position(self):
        service = self._service(exists=True)
        activity = WalletActivity(
            WalletActivityType.BUY,
            WALLET,
            MINT,
            "overlap-signature",
            10,
            PumpFunAddresses.PROGRAM,
            quote_mint=WSOL_MINT,
            token_decimals=6,
        )
        self.assertIsNone(await service.handle(activity))
        self.assertEqual(self.store.list_wallet_events()[0]["state"], "ignored")

    async def test_percentage_source_requires_exact_source_amount(self):
        proportional = TrackedWalletAction(
            True,
            CopySizingMode.PERCENTAGE_OF_SOURCE,
            percentage_bps=BasisPoints(2_500),
        )
        wallet = TrackedWallet(WALLET, False, True, proportional, proportional)
        service = TrackedWalletService(
            WalletTrackingConfig(
                True, DuplicatePolicy.ALLOW_ADDITIONAL_COPY, (wallet,)
            ),
            self.async_store,
            PositionLookup(),
            self._execute,
        )
        activity = WalletActivity(
            WalletActivityType.BUY,
            WALLET,
            MINT,
            "proportional",
            10,
            PumpFunAddresses.PROGRAM,
            quote_mint=WSOL_MINT,
            source_quote_amount=QuoteAmountRaw(10_003, WSOL_MINT, 9),
            token_decimals=6,
        )
        intent = await service.handle(activity)
        self.assertEqual(intent.quote_amount.value, 2_500)

    async def test_fixed_sizing_fails_closed_on_quote_mint_mismatch(self):
        service = self._service()
        activity = WalletActivity(
            WalletActivityType.BUY,
            WALLET,
            MINT,
            "usdc-quote",
            10,
            PumpFunAddresses.PROGRAM,
            quote_mint=USDC,
            source_quote_amount=QuoteAmountRaw(10_000, USDC, 6),
            token_decimals=6,
        )
        self.assertIsNone(await service.handle(activity))
        self.assertEqual(self.executed, [])

    async def test_bounded_processor_does_not_spawn_per_event(self):
        class Decoder:
            def decode(self, _observation, _wallets):
                return None

        processor = TrackedWalletProcessor(
            Decoder(), {WALLET}, lambda *_: None, maximum_pending_events=2, workers=1
        )
        await processor.start()
        self.assertEqual(len(processor._workers), 1)
        await processor.close()


class DecoderTests(unittest.TestCase):
    def setUp(self):
        self.idl = IDLParser(str(ROOT / "idl" / "pump_fun_idl.json"))

    def test_failed_transaction_is_never_decoded(self):
        decoder = PumpFunWalletActivityDecoder(self.idl)
        result = decoder.decode(
            WalletTransactionObservation("sig", 1, (), True, "geyser"), {WALLET}
        )
        self.assertIsNone(result)

    def test_buy_event_requires_tracked_user_and_buy_direction(self):
        class FakeIdl:
            def get_event_discriminators(self):
                return self_idl.get_event_discriminators()

            def get_instruction_discriminators(self):
                return self_idl.get_instruction_discriminators()

            def decode_event_data(self, _raw, event_name=None):
                if event_name == "TradeEvent":
                    return {
                        "event_name": "TradeEvent",
                        "fields": {
                            "user": str(WALLET),
                            "mint": str(MINT),
                            "is_buy": True,
                            "quote_mint": str(WSOL_MINT),
                            "quote_amount": 9_000,
                            "token_amount": 8_000,
                        },
                    }
                return None

        self_idl = self.idl
        decoder = PumpFunWalletActivityDecoder(FakeIdl())
        decoded = decoder.decode(
            WalletTransactionObservation(
                "sig", 44, ("Program data: AA==",), False, "riptide"
            ),
            {WALLET},
        )
        self.assertEqual(decoded[0].activity_type, WalletActivityType.BUY)
        self.assertEqual(decoded[0].source_quote_amount.value, 9_000)
        self.assertEqual(decoded[0].source_token_amount.value, 8_000)


class LaunchBuilderTests(unittest.TestCase):
    def setUp(self):
        self.idl = IDLParser(str(ROOT / "idl" / "pump_fun_idl.json"))
        self.builder = PumpFunLaunchInstructionBuilder(self.idl)

    def test_create_v2_matches_idl_order_and_flags(self):
        instruction = self.builder.build_create_v2(
            mint=MINT,
            user=WALLET,
            creator=WALLET,
            name="Hunter",
            symbol="HUNT",
            uri="https://example.invalid/token.json",
            is_mayhem_mode=False,
            is_cashback_enabled=False,
        )
        definition = next(
            item for item in self.idl.idl["instructions"] if item["name"] == "create_v2"
        )
        self.assertEqual(len(instruction.accounts), len(definition["accounts"]))
        self.assertEqual(
            [(item.is_writable, item.is_signer) for item in instruction.accounts],
            [
                (item.get("writable", False), item.get("signer", False))
                for item in definition["accounts"]
            ],
        )
        self.assertEqual(instruction.data[:8], bytes(definition["discriminator"]))
        self.assertEqual(instruction.data[-2:], b"\x00\x00")

    def test_non_mayhem_still_has_mandatory_mayhem_accounts(self):
        instruction = self.builder.build_create_v2(
            mint=MINT,
            user=WALLET,
            creator=WALLET,
            name="A",
            symbol="A",
            uri="ipfs://cid",
            is_mayhem_mode=False,
            is_cashback_enabled=False,
        )
        self.assertEqual(len(instruction.accounts), 16)

    def test_create_and_buy_are_distinct_packet_components(self):
        plan = LaunchExecutionPlan.from_request(launch_request())
        self.assertEqual(
            [item.action for item in plan.components[:2]], ["create", "buy"]
        )
        self.assertNotEqual(
            plan.components[0].logical_execution_id,
            plan.components[1].logical_execution_id,
        )


def launch_request(additional=()):
    return TokenLaunchRequest(
        "Hunter",
        "HUNT",
        "https://example.invalid/token.json",
        MINT,
        SignerReference("mint", MINT),
        "creator",
        QuoteAmountRaw(1_000, WSOL_MINT, 9),
        tuple(additional),
        launch_id="launch-one",
    )


class FakeTransport:
    def __init__(self, payload=None):
        self.payload = payload or {"result": "bundle-id"}
        self.calls = []

    async def post_json(self, endpoint, payload, **kwargs):
        self.calls.append((endpoint, payload, kwargs))
        return TransportResponse(200, self.payload, {})

    async def close(self):
        return None


def jito_endpoint():
    return ProviderEndpoint(
        "jito",
        ProviderKind.JITO,
        "https://mainnet.block-engine.jito.wtf/api/v1/bundles",
        minimum_tip_lamports=1_000,
        maximum_tip_lamports=10_000,
    )


def contexts(count, tip=1_000):
    blockhash = BlockhashContext.observed("hash", 100)
    return tuple(
        ExecutionContext(
            "trade",
            f"execution-{index}",
            "jito_tipped",
            blockhash,
            f"sig-{index}",
            jito_tip_lamports=tip if index == count - 1 else 0,
        )
        for index in range(count)
    )


class BundleTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_bundle_preserves_order_and_distinct_signatures(self):
        transport = FakeTransport()
        submitter = JitoBundleSubmitter(jito_endpoint(), transport)
        transactions = tuple(
            SignedTransaction(f"wire-{index}".encode(), f"sig-{index}")
            for index in range(3)
        )
        result = await submitter.submit_bundle(
            plan_id="plan",
            transactions=transactions,
            contexts=contexts(3),
            bundle_tip_lamports=1_000,
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.component_signatures, ("sig-0", "sig-1", "sig-2"))
        self.assertEqual(transport.calls[0][1]["method"], "sendBundle")
        self.assertEqual(len(transport.calls[0][1]["params"][0]), 3)

    async def test_bundle_capacity_and_duplicate_signature_fail_closed(self):
        submitter = JitoBundleSubmitter(jito_endpoint(), FakeTransport())
        six = tuple(SignedTransaction(b"x", f"sig-{index}") for index in range(6))
        with self.assertRaisesRegex(ValueError, "one and five"):
            await submitter.submit_bundle(
                plan_id="plan",
                transactions=six,
                contexts=contexts(6),
                bundle_tip_lamports=1_000,
            )
        duplicate = (SignedTransaction(b"a", "same"), SignedTransaction(b"b", "same"))
        with self.assertRaisesRegex(ValueError, "distinct"):
            await submitter.submit_bundle(
                plan_id="plan",
                transactions=duplicate,
                contexts=contexts(2),
                bundle_tip_lamports=1_000,
            )

    async def test_tip_is_separate_and_may_appear_once(self):
        submitter = JitoBundleSubmitter(jito_endpoint(), FakeTransport())
        bad = list(contexts(2))
        bad[0] = ExecutionContext(
            "trade",
            "one",
            "jito_tipped",
            bad[0].blockhash,
            "sig-0",
            jito_tip_lamports=1_000,
        )
        with self.assertRaisesRegex(ValueError, "exactly once"):
            await submitter.submit_bundle(
                plan_id="plan",
                transactions=(
                    SignedTransaction(b"a", "sig-0"),
                    SignedTransaction(b"b", "sig-1"),
                ),
                contexts=tuple(bad),
                bundle_tip_lamports=1_000,
            )

    async def test_bundle_observation_does_not_submit(self):
        transport = FakeTransport(
            {
                "result": {
                    "value": [
                        {"slot": 22, "confirmation_status": "confirmed", "err": None}
                    ]
                }
            }
        )
        observation = await JitoBundleSubmitter(
            jito_endpoint(), transport
        ).observe_bundle("bundle")
        self.assertEqual(observation.state, BundleObservationState.LANDED)
        self.assertEqual(transport.calls[0][1]["method"], "getBundleStatuses")


class LaunchRiskTests(unittest.TestCase):
    def setUp(self):
        self.request = launch_request(
            (LaunchBuy("wallet-a", QuoteAmountRaw(500, WSOL_MINT, 9)),)
        )
        self.plan = LaunchExecutionPlan.from_request(self.request)
        self.cost = LaunchCostEstimate(100, 1_000, 10_000, 20_000)
        self.limits = LaunchRiskLimits(
            True,
            maximum_creator_buy_raw=2_000,
            maximum_additional_wallet_buy_raw=1_000,
            maximum_aggregate_launch_spend_raw=3_000,
            maximum_wallet_count=3,
            maximum_total_priority_fees_lamports=1_000,
            maximum_bundle_tip_lamports=2_000,
            maximum_combined_transaction_cost_lamports=50_000,
            minimum_wallet_reserve_lamports=100,
            maximum_simultaneous_launch_exposure_raw=10_000,
        )

    def test_all_caps_pass_when_balances_are_sufficient(self):
        LaunchRiskService(self.limits).assess(
            self.request,
            self.plan,
            wallet_balances={"creator": 100_000, "wallet-a": 100_000},
            estimated_cost=self.cost,
            bundle_capacity=5,
            active_launch_exposure_raw=0,
        )

    def test_risk_enforcement_is_mandatory(self):
        with self.assertRaises(ExecutionError) as caught:
            LaunchRiskService(LaunchRiskLimits()).assess(
                self.request,
                self.plan,
                wallet_balances={},
                estimated_cost=self.cost,
                bundle_capacity=5,
                active_launch_exposure_raw=0,
            )
        self.assertEqual(
            caught.exception.classification, ErrorClassification.RISK_LIMIT_EXCEEDED
        )

    def test_bundle_capacity_is_checked_before_signing(self):
        with self.assertRaisesRegex(ExecutionError, "capacity"):
            LaunchRiskService(self.limits).assess(
                self.request,
                self.plan,
                wallet_balances={"creator": 100_000, "wallet-a": 100_000},
                estimated_cost=self.cost,
                bundle_capacity=2,
                active_launch_exposure_raw=0,
            )

    def test_launch_quote_mints_and_decimals_must_match(self):
        with self.assertRaisesRegex(ValueError, "quote mint and decimals"):
            launch_request((LaunchBuy("wallet-a", QuoteAmountRaw(500, USDC, 6)),))

    def test_spl_quoted_launch_fails_closed_before_signing(self):
        request = TokenLaunchRequest(
            "Hunter",
            "HUNT",
            "https://example.invalid/token.json",
            MINT,
            SignerReference("mint", MINT),
            "creator",
            QuoteAmountRaw(1_000, USDC, 6),
            launch_id="spl-launch",
        )
        with self.assertRaises(ExecutionError) as caught:
            LaunchRiskService(self.limits).assess(
                request,
                LaunchExecutionPlan.from_request(request),
                wallet_balances={"creator": 100_000},
                estimated_cost=self.cost,
                bundle_capacity=5,
                active_launch_exposure_raw=0,
            )
        self.assertEqual(
            caught.exception.classification,
            ErrorClassification.UNSUPPORTED_QUOTE_TOKEN,
        )


class FakeSigners:
    def __init__(self):
        self.keys = {"creator": WALLET, "wallet-a": OTHER, "mint": MINT}

    def public_key(self, signer_id):
        return self.keys[signer_id]

    async def sign_instructions(
        self, *, instructions, fee_payer_id, required_signer_ids, blockhash
    ):
        del instructions, required_signer_ids, blockhash
        return SignedTransaction(
            f"wire:{fee_payer_id}".encode(), f"signature:{fee_payer_id}"
        )


class FakeBalances:
    def __init__(self, value=1_000_000):
        self.value = value

    async def native_balance_lamports(self, _wallet):
        return self.value


class FakeBlockhashes:
    async def get_blockhash(self):
        return BlockhashContext.observed("launch-hash", 500)


class FakePreparer:
    async def prepare(
        self,
        request,
        component,
        blockhash,
        *,
        execution_variant,
        compute_unit_limit,
        compute_unit_price_micro_lamports,
        priority_fee_lamports,
        jito_tip_lamports,
    ):
        del request
        transaction = SignedTransaction(
            f"wire:{component.component_id}".encode(),
            f"signature:{component.component_id}",
        )
        context = ExecutionContext(
            "launch",
            component.logical_execution_id,
            execution_variant,
            blockhash,
            transaction.signature,
            compute_unit_limit=compute_unit_limit,
            compute_unit_price_micro_lamports=compute_unit_price_micro_lamports,
            priority_fee_lamports=priority_fee_lamports,
            jito_tip_lamports=jito_tip_lamports,
        )
        return PreparedLaunchComponent(component, transaction, context)


class LaunchServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = SQLitePositionStore(":memory:")
        self.signers = FakeSigners()
        self.wallets = (
            FleetWallet(
                "creator", SignerReference("creator", WALLET), FleetWalletRole.CREATOR
            ),
            FleetWallet(
                "wallet-a",
                SignerReference("wallet-a", OTHER),
                FleetWalletRole.PARTICIPANT,
            ),
        )
        self.limits = LaunchRiskLimits(
            True,
            10_000,
            10_000,
            20_000,
            5,
            10_000,
            10_000,
            100_000,
            100,
            100_000,
        )
        self.transport = FakeTransport()
        self.bundle = JitoBundleSubmitter(jito_endpoint(), self.transport)

    async def asyncTearDown(self):
        self.store.close()

    def _service(self, balances=None, submission_authorizer=None):
        async def submit(transaction, context):
            return SubmissionResult(
                transaction.signature,
                "rpc",
                "rpc.invalid",
                execution_variant=context.execution_variant,
            )

        return TokenLaunchService(
            wallets=self.wallets,
            signer_registry=self.signers,
            balance_reader=balances or FakeBalances(),
            blockhash_provider=FakeBlockhashes(),
            preparer=FakePreparer(),
            risk_service=LaunchRiskService(self.limits),
            store=self.store,
            component_submitter=submit,
            bundle_submitter=self.bundle,
            submission_authorizer=submission_authorizer,
        )

    async def test_bundle_plan_is_persisted_before_acknowledgement(self):
        result = await self._service().execute(
            launch_request(),
            estimated_cost=LaunchCostEstimate(100, 1_000, 10_000, 20_000),
            execution_variant="jito_tipped",
        )
        self.assertEqual(result.state, LaunchState.SUBMITTED)
        stored = self.store.get_launch_plan(result.plan.plan_id)
        self.assertEqual(stored["bundle_id"], "bundle-id")
        components = self.store.list_launch_components(result.plan.plan_id)
        self.assertEqual(len({item["signature"] for item in components}), 2)
        self.assertEqual({item["state"] for item in components}, {"submitted"})

    async def test_plan_idempotency_refuses_second_economic_launch(self):
        service = self._service()
        request = launch_request()
        cost = LaunchCostEstimate(100, 1_000, 10_000, 20_000)
        await service.execute(
            request, estimated_cost=cost, execution_variant="jito_tipped"
        )
        with self.assertRaisesRegex(ExecutionError, "inspect before retry"):
            await service.execute(
                request, estimated_cost=cost, execution_variant="jito_tipped"
            )

    async def test_balance_rejection_occurs_before_signing(self):
        service = self._service(FakeBalances(0))
        with self.assertRaises(ExecutionError) as caught:
            await service.execute(
                launch_request(),
                estimated_cost=LaunchCostEstimate(100, 1_000, 10_000, 20_000),
                execution_variant="jito_tipped",
            )
        self.assertEqual(
            caught.exception.classification, ErrorClassification.INSUFFICIENT_BALANCE
        )
        self.assertEqual(self.store.list_launch_components("launch:launch-one"), [])

    async def test_recovery_inspects_bundle_without_resubmission(self):
        service = self._service()
        result = await service.execute(
            launch_request(),
            estimated_cost=LaunchCostEstimate(100, 1_000, 10_000, 20_000),
            execution_variant="jito_tipped",
        )
        self.transport.payload = {
            "result": {
                "value": [{"slot": 99, "confirmation_status": "confirmed", "err": None}]
            }
        }
        recovered = await TokenLaunchRecoveryService(
            self.store, bundle_submitter=self.bundle
        ).recover_plan(result.plan.plan_id)
        self.assertEqual(recovered, LaunchState.LANDED)
        self.assertEqual(
            [call[1]["method"] for call in self.transport.calls],
            ["sendBundle", "getBundleStatuses"],
        )
        self.assertEqual(
            {
                item["state"]
                for item in self.store.list_launch_components(result.plan.plan_id)
            },
            {"landed"},
        )

    async def test_post_sign_validation_error_requires_reconciliation(self):
        with self.assertRaisesRegex(ValueError, "configured minimum"):
            await self._service().execute(
                launch_request(),
                estimated_cost=LaunchCostEstimate(100, 500, 10_000, 20_000),
                execution_variant="jito_tipped",
            )
        stored = self.store.get_launch_plan("launch:launch-one")
        self.assertEqual(stored["state"], LaunchState.RECONCILIATION_REQUIRED.value)
        self.assertEqual(
            stored["error_classification"], ErrorClassification.UNKNOWN.value
        )

    async def test_runtime_halt_revalidates_prepared_launch_before_submission(self):
        def reject_new_exposure():
            raise ExecutionError(
                ErrorClassification.RISK_LIMIT_EXCEEDED,
                "runtime kill switch blocks new exposure",
            )

        with self.assertRaisesRegex(ExecutionError, "kill switch"):
            await self._service(submission_authorizer=reject_new_exposure).execute(
                launch_request(),
                estimated_cost=LaunchCostEstimate(100, 1_000, 10_000, 20_000),
                execution_variant="jito_tipped",
            )
        self.assertEqual(self.transport.calls, [])
        stored = self.store.get_launch_plan("launch:launch-one")
        self.assertEqual(stored["state"], LaunchState.RECONCILIATION_REQUIRED.value)

    async def test_concrete_preparer_uses_create_builder_and_buy_factory(self):
        idl = IDLParser(str(ROOT / "idl" / "pump_fun_idl.json"))
        buy_calls = []

        async def buy_factory(request, component, wallet):
            buy_calls.append((request, component, wallet))
            return [Instruction(PumpFunAddresses.PROGRAM, b"buy", [])]

        preparer = PumpFunLaunchComponentPreparer(
            PumpFunLaunchInstructionBuilder(idl),
            buy_factory,
            self.signers,
            {item.wallet_id: item for item in self.wallets},
        )
        plan = LaunchExecutionPlan.from_request(launch_request())
        created = await preparer.prepare(
            launch_request(),
            plan.components[0],
            BlockhashContext.observed("hash", 100),
            execution_variant="standard",
            compute_unit_limit=100_000,
            compute_unit_price_micro_lamports=1,
            priority_fee_lamports=1,
            jito_tip_lamports=0,
        )
        bought = await preparer.prepare(
            launch_request(),
            plan.components[1],
            BlockhashContext.observed("hash", 100),
            execution_variant="standard",
            compute_unit_limit=100_000,
            compute_unit_price_micro_lamports=1,
            priority_fee_lamports=1,
            jito_tip_lamports=0,
        )
        self.assertEqual(created.transaction.signature, "signature:creator")
        self.assertEqual(bought.transaction.signature, "signature:creator")
        self.assertEqual(len(buy_calls), 1)


class FleetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = SQLitePositionStore(":memory:")
        self.store.create_launch_plan(
            plan_id="plan",
            launch_id="launch",
            mint=str(MINT),
            state="active",
            execution_policy="parallel_fast",
            exit_policy={"exit_type": "manual"},
        )
        WalletFleetService(self.store).record_buy(
            plan_id="plan",
            launch_id="launch",
            mint=MINT,
            quote_mint=WSOL_MINT,
            wallet_id="creator",
            wallet_role="creator",
            token_decimals=6,
            quote_decimals=9,
            buy_signature="buy-signature",
            acquired_quantity_raw=100,
            quote_cost_basis_raw=1_000,
            known_cost_lamports=100,
            marry_mode=False,
        )

    async def asyncTearDown(self):
        self.store.close()

    def test_profit_target_uses_expected_proceeds_and_known_sol_cost(self):
        positions = self.store.list_fleet_positions("plan")
        valuation = FleetValuation(
            {positions[0]["fleet_position_id"]: 1_300}, WSOL_MINT, 9, datetime.now(UTC)
        )
        decision = FleetExitEvaluator.evaluate(
            positions,
            valuation,
            FleetExitPolicy(FleetExitType.PROFIT_TARGET, target_bps=1_000),
        )
        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.gross_return_bps, 3_000)
        self.assertEqual(decision.net_return_bps, 2_000)

    def test_marry_mode_blocks_automatic_exit(self):
        positions = self.store.list_fleet_positions("plan")
        positions[0]["marry_mode"] = True
        decision = FleetExitEvaluator.evaluate(
            positions,
            FleetValuation(
                {positions[0]["fleet_position_id"]: 9_000},
                WSOL_MINT,
                9,
                datetime.now(UTC),
            ),
            FleetExitPolicy(FleetExitType.PROFIT_TARGET, target_bps=1),
        )
        self.assertFalse(decision.should_exit)

    def test_spl_quote_keeps_sol_network_cost_out_of_net_return(self):
        positions = self.store.list_fleet_positions("plan")
        positions[0]["quote_mint"] = str(USDC)
        valuation = FleetValuation(
            {positions[0]["fleet_position_id"]: 1_300},
            USDC,
            6,
            datetime.now(UTC),
        )
        decision = FleetExitEvaluator.evaluate(
            positions,
            valuation,
            FleetExitPolicy(FleetExitType.PROFIT_TARGET, target_bps=1_000),
        )
        self.assertTrue(decision.should_exit)
        self.assertEqual(decision.gross_return_bps, 3_000)
        self.assertIsNone(decision.net_return_bps)

    def test_persisted_time_exit_survives_restart(self):
        positions = self.store.list_fleet_positions("plan")
        positions[0]["scheduled_exit_at"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
        decision = FleetExitEvaluator.evaluate(
            positions,
            FleetValuation(
                {positions[0]["fleet_position_id"]: 1_000},
                WSOL_MINT,
                9,
                datetime.now(UTC),
            ),
            FleetExitPolicy(FleetExitType.TIME_BASED, after_seconds=60),
        )
        self.assertTrue(decision.should_exit)

    async def test_exit_has_stable_identity_and_no_duplicate_sell(self):
        executed = []

        async def execute(intent):
            executed.append(intent)
            return type("Result", (), {"signature": "sell-signature"})()

        service = WalletFleetExitService(self.store, execute)
        first = await service.execute_exit(
            plan_id="plan",
            trigger=FleetExitType.MANUAL,
            policy=FleetExecutionPolicy.PARALLEL_FAST,
            slippage=BasisPoints(100),
        )
        second = await service.execute_exit(
            plan_id="plan",
            trigger=FleetExitType.MANUAL,
            policy=FleetExecutionPolicy.PARALLEL_FAST,
            slippage=BasisPoints(100),
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(second, ())
        self.assertEqual(len(executed), 1)

    async def test_partial_fleet_sells_allocate_cost_and_allow_remaining_exit(self):
        async def execute(_intent):
            return type("Result", (), {"signature": "submitted"})()

        exits = WalletFleetExitService(self.store, execute)
        accounting = WalletFleetService(self.store)
        first = await exits.execute_exit(
            plan_id="plan",
            trigger=FleetExitType.MANUAL,
            policy=FleetExecutionPolicy.SEQUENTIAL,
            slippage=BasisPoints(100),
        )
        partial = accounting.record_sell(
            first[0].intent_id,
            signature="sell-partial",
            sold_quantity_raw=40,
            quote_proceeds_raw=500,
            known_exit_cost_lamports=20,
        )
        self.assertEqual(partial["remaining_quantity_raw"], 60)
        self.assertEqual(partial["quote_cost_basis_raw"], 600)
        self.assertEqual(partial["realized_pnl_raw"], 100)
        self.assertEqual(partial["known_cost_lamports"], 120)

        second = await exits.execute_exit(
            plan_id="plan",
            trigger=FleetExitType.MANUAL,
            policy=FleetExecutionPolicy.SEQUENTIAL,
            slippage=BasisPoints(100),
        )
        self.assertNotEqual(first[0].intent_id, second[0].intent_id)
        closed = accounting.record_sell(
            second[0].intent_id,
            signature="sell-final",
            sold_quantity_raw=60,
            quote_proceeds_raw=700,
            known_exit_cost_lamports=30,
        )
        self.assertEqual(closed["remaining_quantity_raw"], 0)
        self.assertEqual(closed["quote_cost_basis_raw"], 0)
        self.assertEqual(closed["realized_pnl_raw"], 200)
        self.assertEqual(closed["status"], "closed")

    def test_restart_exposes_pending_exit_without_resubmission(self):
        service = WalletFleetExitService(self.store, lambda _intent: None)
        self.store.claim_fleet_exit(
            logical_execution_id="pending",
            plan_id="plan",
            fleet_position_id=self.store.list_fleet_positions("plan")[0][
                "fleet_position_id"
            ],
            trigger_type="manual",
        )
        self.assertEqual(service.pending_after_restart("plan")[0]["state"], "planned")


class PersistenceTests(unittest.TestCase):
    def test_schema_contains_wallet_and_fleet_state_without_secret_columns(self):
        store = SQLitePositionStore(":memory:")
        try:
            self.assertEqual(store.schema_version, SCHEMA_VERSION)
            tables = {
                row[0]
                for row in store._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("tracked_wallet_events", tables)
            self.assertIn("launch_plans", tables)
            self.assertIn("fleet_positions", tables)
            columns = {
                row[1]
                for row in store._connection.execute(
                    "PRAGMA table_info(fleet_positions)"
                )
            }
            self.assertFalse({"private_key", "secret", "token"} & columns)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
