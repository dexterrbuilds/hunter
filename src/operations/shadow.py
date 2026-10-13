"""Passive listener lifecycle without wallets, signers, positions or senders."""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, monotonic_ns
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from execution.detection import detection_for
from operations.config import validate_deployment
from operations.storage import ShadowStore
from utils.logger import configure_safe_console_logging, get_logger

if TYPE_CHECKING:
    from interfaces.core import TokenInfo
    from monitoring.base_listener import BaseTokenListener

logger = get_logger(__name__)
_BATCH_SIZE = 128


class ShadowRuntime:
    """Bounded observation queue; persistence failures terminate the session."""

    def __init__(self, config: dict[str, Any], listener: BaseTokenListener) -> None:
        validate_deployment(config)
        self.config = config
        self.listener = listener
        settings = config["deployment"]
        self.queue: asyncio.Queue[tuple] = asyncio.Queue(
            settings.get("queue_size", 1024)
        )
        self.stop = asyncio.Event()
        self.session_id = str(uuid4())
        self.observed = 0
        self.dropped = 0
        self.last_observed: float | None = None
        self.state = "initializing"
        self.failure: str | None = None
        self.store: ShadowStore | None = None
        self.started = monotonic()
        self.recovered_sessions = 0
        self.recovery_ms: float | None = None

    async def observe(self, token: TokenInfo) -> None:
        """Capture public identifiers and clocks only; never simulate fills."""
        self.observed += 1
        self.last_observed = monotonic()
        self.state = "receiving"
        detection = detection_for(token)
        slot = detection.event_slot if detection else None
        observed = detection.event_observed_at if detection else datetime.now(UTC)
        observed_ns = detection.event_observed_mono_ns if detection else monotonic_ns()
        row = (
            self.session_id,
            str(token.mint),
            self.config["filters"]["listener_type"],
            observed.isoformat(),
            observed_ns,
            slot if type(slot) is int else None,
        )
        try:
            self.queue.put_nowait(row)
        except asyncio.QueueFull:
            self.dropped += 1

    def status(self) -> dict[str, object]:
        age = None if self.last_observed is None else monotonic() - self.last_observed
        state = self.state
        if (
            state == "receiving"
            and age is not None
            and age > self.config["deployment"].get("stale_after_seconds", 120)
        ):
            state = "stale"
        return {
            "session_id": self.session_id,
            "region_label": self.config["deployment"].get("region_label", "amsterdam"),
            "mode": "shadow_non_trading",
            "live_trading": False,
            "feed_state": state,
            "connection_state": "unknown",
            "last_observation_age_seconds": age,
            "observed": self.observed,
            "queue_depth": self.queue.qsize(),
            "dropped": self.dropped,
            "failure": self.failure,
            "recovered_observation_sessions": self.recovered_sessions,
            "recovery_ms": self.recovery_ms,
            "uptime_seconds": monotonic() - self.started,
            "provider_connectivity_tested": False,
            "blockhash_cache": "disabled",
            "fee_cache": "disabled",
            "tip_cache": "disabled",
            "reconnect_attempts": None,
        }

    async def _persist(self) -> None:
        while True:
            row = await self.queue.get()
            batch = [row]
            while len(batch) < _BATCH_SIZE and not self.queue.empty():
                batch.append(self.queue.get_nowait())
            try:
                await asyncio.to_thread(self.store.record, batch)
            finally:
                for _row in batch:
                    self.queue.task_done()

    async def _report(self) -> None:
        while True:
            logger.info("shadow_status %s", self.status())
            await asyncio.sleep(
                self.config["deployment"].get("report_interval_seconds", 30)
            )

    async def run(self) -> None:
        """Initialize/recover storage before listening; flush within a bound."""
        tasks: list[asyncio.Task] = []
        clean = False
        try:
            recovery_started = monotonic()
            self.store = await asyncio.to_thread(
                ShadowStore, Path(self.config["storage"]["shadow_database_path"])
            )
            self.recovered_sessions = await asyncio.to_thread(
                self.store.start, self.session_id, datetime.now(UTC).isoformat()
            )
            self.recovery_ms = (monotonic() - recovery_started) * 1000
            self.state = "unknown"  # Listener constructors do not prove connection.
            writer = asyncio.create_task(self._persist(), name="shadow-persistence")
            detector = asyncio.create_task(
                self.listener.listen_for_tokens(self.observe), name="shadow-detection"
            )
            reporter = asyncio.create_task(self._report(), name="shadow-status")
            stopper = asyncio.create_task(self.stop.wait(), name="shadow-stop")
            tasks = [writer, detector, reporter, stopper]
            done, _pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is not stopper:
                    task.result()
                    raise RuntimeError("shadow service unexpectedly stopped")  # noqa: TRY003, TRY301
            detector.cancel()
            await asyncio.gather(detector, return_exceptions=True)
            await asyncio.wait_for(
                self.queue.join(),
                self.config["deployment"].get("shutdown_timeout_seconds", 10),
            )
            if writer.done():
                writer.result()
            clean = True
        except BaseException as error:
            self.state = "failed"
            self.failure = type(error).__name__
            raise
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.store is not None:
                # Serialize close after any in-flight writer thread via its lock.
                if clean:
                    await asyncio.to_thread(
                        self.store.finish,
                        self.session_id,
                        datetime.now(UTC).isoformat(),
                    )
                await asyncio.to_thread(self.store.close)
            if clean:
                self.state = "stopped"
            logger.info("shadow_shutdown %s", self.status())


async def run_shadow(config: dict[str, Any]) -> None:
    """Build existing listeners only; attach process termination handlers."""
    from interfaces.core import Platform  # noqa: PLC0415
    from monitoring.listener_factory import ListenerFactory  # noqa: PLC0415

    validate_deployment(config)
    configure_safe_console_logging()
    listener = ListenerFactory.create_listener(
        config["filters"]["listener_type"],
        wss_endpoint=config.get("wss_endpoint"),
        pumpportal_url=config.get("pumpportal", {}).get(
            "url", "wss://pumpportal.fun/api/data"
        ),
        platforms=[Platform(config.get("platform", "pump_fun"))],
    )
    runtime = ShadowRuntime(config, listener)
    loop = asyncio.get_running_loop()
    signals = (signal.SIGTERM, signal.SIGINT)
    for item in signals:
        loop.add_signal_handler(item, runtime.stop.set)
    try:
        await runtime.run()
    finally:
        for item in signals:
            loop.remove_signal_handler(item)
