"""Streaming Pump.fun wallet activity decoding and bounded dispatch."""

# ruff: noqa: C901, PLC0415, PLR0913, TC001

from __future__ import annotations

import asyncio
import base64
import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic_ns

from solders.pubkey import Pubkey

from core.pubkeys import TOKEN_DECIMALS, normalize_quote_mint, quote_decimals
from domain.amounts import QuoteAmountRaw, TokenAmountRaw
from domain.wallet_tracking import WalletActivity, WalletActivityType
from interfaces.core import TokenInfo
from platforms.pumpfun.address_provider import PumpFunAddresses
from platforms.pumpfun.event_parser import PumpFunEventParser
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)

ActivityHandler = Callable[[WalletActivity, TokenInfo | None], Awaitable[None]]
StateObserver = Callable[[str, str | None], None]


@dataclass(frozen=True, slots=True)
class WalletTransactionObservation:
    """Provider-neutral transaction evidence delivered by a streaming feed."""

    signature: str
    slot: int
    logs: tuple[str, ...]
    failed: bool
    source: str


class PumpFunWalletActivityDecoder:
    """Decode authoritative Anchor events; unrelated/failed activity is ignored."""

    def __init__(self, idl_parser: IDLParser) -> None:
        self._idl = idl_parser
        self._creation_parser = PumpFunEventParser(idl_parser)

    def decode(
        self,
        observation: WalletTransactionObservation,
        tracked_wallets: set[Pubkey],
    ) -> tuple[WalletActivity, TokenInfo | None] | None:
        if observation.failed:
            return None
        created = self._creation_parser.parse_token_creation_from_logs(
            list(observation.logs), observation.signature
        )
        if created is not None and created.creator in tracked_wallets:
            return (
                WalletActivity(
                    activity_type=WalletActivityType.CREATE,
                    wallet=created.creator,
                    mint=created.mint,
                    signature=observation.signature,
                    slot=observation.slot,
                    program_id=PumpFunAddresses.PROGRAM,
                    quote_mint=normalize_quote_mint(created.quote_mint),
                    token_decimals=TOKEN_DECIMALS,
                    source=observation.source,
                    decoded_mono_ns=monotonic_ns(),
                ),
                created,
            )

        for log in observation.logs:
            if "Program data:" not in log:
                continue
            try:
                raw = base64.b64decode(log.split("Program data:", 1)[1].strip())
            except (ValueError, TypeError):
                continue
            decoded = self._idl.decode_event_data(raw, "TradeEvent")
            if not decoded:
                continue
            fields = decoded.get("fields", {})
            if fields.get("is_buy") is not True:
                continue
            try:
                user = Pubkey.from_string(str(fields["user"]))
                mint = Pubkey.from_string(str(fields["mint"]))
                quote_value = fields.get("quote_mint")
                quote_mint = normalize_quote_mint(
                    Pubkey.from_string(quote_value)
                    if isinstance(quote_value, str)
                    else quote_value
                )
                quote_raw = fields.get("quote_amount", fields.get("sol_amount"))
                token_raw = fields.get("token_amount")
                if user not in tracked_wallets:
                    continue
                if not isinstance(quote_raw, int) or not isinstance(token_raw, int):
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            return (
                WalletActivity(
                    activity_type=WalletActivityType.BUY,
                    wallet=user,
                    mint=mint,
                    signature=observation.signature,
                    slot=observation.slot,
                    program_id=PumpFunAddresses.PROGRAM,
                    quote_mint=quote_mint,
                    source_quote_amount=QuoteAmountRaw(
                        quote_raw, quote_mint, quote_decimals(quote_mint)
                    ),
                    source_token_amount=TokenAmountRaw(token_raw, mint, TOKEN_DECIMALS),
                    token_decimals=TOKEN_DECIMALS,
                    source=observation.source,
                    decoded_mono_ns=monotonic_ns(),
                ),
                None,
            )
        return None


class TrackedWalletProcessor:
    """Bounded streaming decoder; no polling or unbounded task creation."""

    def __init__(
        self,
        decoder: PumpFunWalletActivityDecoder,
        tracked_wallets: set[Pubkey],
        handler: ActivityHandler,
        *,
        maximum_pending_events: int = 256,
        workers: int = 2,
    ) -> None:
        self.decoder = decoder
        self.tracked_wallets = frozenset(tracked_wallets)
        self.handler = handler
        self.queue: asyncio.Queue[WalletTransactionObservation | None] = asyncio.Queue(
            maxsize=maximum_pending_events
        )
        self.worker_count = workers
        self._workers: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        if self._workers:
            return
        self._workers = [
            asyncio.create_task(self._run(), name=f"wallet-decoder-{index}")
            for index in range(self.worker_count)
        ]

    async def submit(self, observation: WalletTransactionObservation) -> None:
        await self.queue.put(observation)

    async def close(self) -> None:
        if not self._workers:
            return
        for _ in self._workers:
            await self.queue.put(None)
        await asyncio.gather(*self._workers)
        self._workers.clear()

    async def _run(self) -> None:
        while True:
            observation = await self.queue.get()
            try:
                if observation is None:
                    return
                decoded = self.decoder.decode(observation, set(self.tracked_wallets))
                if decoded is not None:
                    await self.handler(*decoded)
            finally:
                self.queue.task_done()


class TrackedWalletLogSource:
    """Portable processed log stream for explicitly configured addresses.

    Solana's core ``logsSubscribe`` permits one ``mentions`` address per
    subscription, so each tracked address owns one bounded reconnecting stream.
    Faster Geyser/shred adapters can emit the same observation model directly.
    """

    def __init__(
        self,
        wss_endpoint: str,
        wallets: tuple[Pubkey, ...],
        processor: TrackedWalletProcessor,
        *,
        reconnect_delay_seconds: float = 1.0,
        maximum_reconnect_delay_seconds: float = 30.0,
        state_observer: StateObserver | None = None,
    ) -> None:
        self.wss_endpoint = wss_endpoint
        self.wallets = wallets
        self.processor = processor
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self.maximum_reconnect_delay_seconds = maximum_reconnect_delay_seconds
        self.state_observer = state_observer
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        if self._tasks:
            return
        self._observe("connecting")
        await self.processor.start()
        self._tasks = [
            asyncio.create_task(
                self._listen(wallet), name=f"tracked-wallet-{str(wallet)[:8]}"
            )
            for wallet in self.wallets
        ]
        for task in self._tasks:
            task.add_done_callback(self._task_completed)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.processor.close()
        self._observe("stopped")

    def _observe(self, state: str, reason: str | None = None) -> None:
        if self.state_observer is not None:
            self.state_observer(state, reason)

    def _task_completed(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._observe("failed", type(error).__name__)

    async def _listen(self, wallet: Pubkey) -> None:
        import websockets

        delay = self.reconnect_delay_seconds
        while True:
            try:
                async with websockets.connect(self.wss_endpoint) as websocket:
                    await websocket.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": str(wallet),
                                "method": "logsSubscribe",
                                "params": [
                                    {"mentions": [str(wallet)]},
                                    {"commitment": "processed"},
                                ],
                            }
                        )
                    )
                    await websocket.recv()
                    delay = self.reconnect_delay_seconds
                    self._observe("connected")
                    while True:
                        message = json.loads(await websocket.recv())
                        params = message.get("params", {})
                        result = params.get("result", {})
                        context = result.get("context", {})
                        value = result.get("value", {})
                        signature = value.get("signature")
                        logs = value.get("logs")
                        slot = context.get("slot")
                        if (
                            isinstance(signature, str)
                            and isinstance(slot, int)
                            and isinstance(logs, list)
                            and all(isinstance(item, str) for item in logs)
                        ):
                            self._observe("receiving")
                            await self.processor.submit(
                                WalletTransactionObservation(
                                    signature=signature,
                                    slot=slot,
                                    logs=tuple(logs),
                                    failed=value.get("err") is not None,
                                    source="logs_subscribe",
                                )
                            )
            except asyncio.CancelledError:
                raise
            except websockets.ConnectionClosed:
                self._observe("degraded", "connection_closed")
                await asyncio.sleep(delay + random.uniform(0, delay * 0.1))  # noqa: S311
                delay = min(delay * 2, self.maximum_reconnect_delay_seconds)
            except (json.JSONDecodeError, OSError, TimeoutError) as error:
                logger.warning(
                    "Tracked-wallet stream reconnecting after %s",
                    type(error).__name__,
                )
                self._observe("degraded", type(error).__name__)
                await asyncio.sleep(delay + random.uniform(0, delay * 0.1))  # noqa: S311
                delay = min(delay * 2, self.maximum_reconnect_delay_seconds)
