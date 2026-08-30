"""Bounded UDP ingress for provider/sidecar-reconstructed Triton shred data.

Solana shred reconstruction is erasure-code and fork aware. Hunter therefore
does not guess a proprietary packet format: a provider SDK or colocated
reconstruction sidecar supplies the ``ShredReconstructor`` implementation.
The included framed decoder is for an explicit sidecar envelope containing one
already-reconstructed transaction, not for raw Solana shreds.
"""

# The adapter keeps explicit provider/worker inputs and direct validation errors.
# ruff: noqa: PLR0913, TRY003

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, field
from time import monotonic_ns
from typing import TYPE_CHECKING, Protocol

from solders.signature import Signature

from execution.detection import record_detection
from execution.telemetry import utc_now
from monitoring.base_listener import BaseTokenListener

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from interfaces.core import TokenInfo

MAX_UDP_PORT = 65_535


@dataclass(frozen=True, slots=True)
class ShredPacket:
    """One UDP packet captured before any parsing or queueing work."""

    payload: bytes
    received_mono_ns: int
    peer: tuple[str, int] | None = None
    received_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class ReconstructedTransaction:
    """Transaction bytes and identity emitted by a verified reconstructor."""

    slot: int
    signature: str
    wire_bytes: bytes


class ShredReconstructor(Protocol):
    """Provider SDK/sidecar boundary for correct shred reconstruction."""

    def feed(self, packet: ShredPacket) -> list[ReconstructedTransaction]: ...


class PumpCreationRecognizer(Protocol):
    """Recognize a Pump.fun creation from reconstructed transaction bytes."""

    def recognize(self, transaction: ReconstructedTransaction) -> TokenInfo | None: ...


class FramedTransactionReconstructor:
    """Decode Hunter's documented sidecar transaction envelope.

    Layout: ``HNTR`` magic, little-endian u64 slot, 64-byte signature, then
    little-endian u32 transaction length and the exact wire transaction bytes.
    """

    HEADER = struct.Struct("<4sQ64sI")

    def feed(self, packet: ShredPacket) -> list[ReconstructedTransaction]:
        if len(packet.payload) < self.HEADER.size:
            return []
        magic, slot, signature_bytes, length = self.HEADER.unpack_from(packet.payload)
        if magic != b"HNTR" or length <= 0:
            return []
        end = self.HEADER.size + length
        if end != len(packet.payload):
            return []
        signature = str(Signature.from_bytes(signature_bytes))
        return [
            ReconstructedTransaction(
                slot=slot,
                signature=signature,
                wire_bytes=packet.payload[self.HEADER.size : end],
            )
        ]


@dataclass(slots=True)
class ShredIngressStats:
    """Bounded-feed health counters safe to expose in readiness telemetry."""

    packets_received: int = 0
    packets_dropped: int = 0
    reconstructed_transactions: int = 0
    malformed_or_incomplete: int = 0
    launches_observed: int = 0
    reconnects: int = 0


class _DatagramIngress(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue[ShredPacket]) -> None:
        self.queue = queue
        self.transport: asyncio.DatagramTransport | None = None
        self.closed: asyncio.Future[None] | None = None
        self.stats = ShredIngressStats()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport
        self.closed = asyncio.get_running_loop().create_future()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        packet = ShredPacket(
            bytes(data),
            monotonic_ns(),
            addr,
            utc_now(),
        )
        self.stats.packets_received += 1
        try:
            self.queue.put_nowait(packet)
        except asyncio.QueueFull:
            self.stats.packets_dropped += 1

    def connection_lost(self, exc: Exception | None) -> None:
        del exc
        if self.closed is not None and not self.closed.done():
            self.closed.set_result(None)


class TritonShredListener(BaseTokenListener):
    """Reconnect-safe UDP listener with bounded reconstruction workers."""

    def __init__(
        self,
        host: str,
        port: int,
        reconstructor: ShredReconstructor,
        recognizer: PumpCreationRecognizer,
        *,
        region: str | None = None,
        queue_size: int = 8_192,
        worker_count: int = 2,
        reconnect_delay_seconds: float = 1.0,
    ) -> None:
        super().__init__()
        if not host or not 0 < port <= MAX_UDP_PORT:
            raise ValueError("Triton shred ingress requires a valid UDP host/port")
        if queue_size <= 0 or worker_count <= 0 or reconnect_delay_seconds <= 0:
            raise ValueError("Triton shred worker bounds must be positive")
        self.host = host
        self.port = port
        self.reconstructor = reconstructor
        self.recognizer = recognizer
        self.region = region
        self.queue_size = queue_size
        self.worker_count = worker_count
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self.stats = ShredIngressStats()

    async def listen_for_tokens(
        self,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: str | None = None,
        creator_address: str | None = None,
    ) -> None:
        queue: asyncio.Queue[ShredPacket] = asyncio.Queue(maxsize=self.queue_size)
        workers = [
            asyncio.create_task(
                self._worker(queue, token_callback, match_string, creator_address),
                name=f"hunter-triton-shred-parser-{index}",
            )
            for index in range(self.worker_count)
        ]
        loop = asyncio.get_running_loop()
        try:
            while True:
                protocol = _DatagramIngress(queue)
                transport, _ = await loop.create_datagram_endpoint(
                    lambda protocol=protocol: protocol,
                    local_addr=(self.host, self.port),
                )
                try:
                    if protocol.closed is not None:
                        await protocol.closed
                finally:
                    transport.close()
                    self._merge_stats(protocol.stats)
                self.stats.reconnects += 1
                await asyncio.sleep(self.reconnect_delay_seconds)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def _worker(
        self,
        queue: asyncio.Queue[ShredPacket],
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: str | None,
        creator_address: str | None,
    ) -> None:
        while True:
            packet = await queue.get()
            try:
                reconstructed = self.reconstructor.feed(packet)
                if not reconstructed:
                    self.stats.malformed_or_incomplete += 1
                for transaction in reconstructed:
                    self.stats.reconstructed_transactions += 1
                    token_info = self.recognizer.recognize(transaction)
                    parser_completed = monotonic_ns()
                    if token_info is None:
                        continue
                    if (
                        match_string
                        and match_string.lower()
                        not in (token_info.name + token_info.symbol).lower()
                    ):
                        continue
                    if creator_address and str(token_info.user) != creator_address:
                        continue
                    self.stats.launches_observed += 1
                    record_detection(
                        token_info,
                        source="triton_shreds",
                        source_region=self.region,
                        event_slot=transaction.slot,
                        transaction_slot=transaction.slot,
                        launch_slot=transaction.slot,
                        transaction_signature=transaction.signature,
                        observed_at=packet.received_at,
                        observed_mono_ns=packet.received_mono_ns,
                        parser_completed_mono_ns=parser_completed,
                        validation_completed_mono_ns=parser_completed,
                    )
                    await token_callback(token_info)
            finally:
                queue.task_done()

    def _merge_stats(self, value: ShredIngressStats) -> None:
        self.stats.packets_received += value.packets_received
        self.stats.packets_dropped += value.packets_dropped
