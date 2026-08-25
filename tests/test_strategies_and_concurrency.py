from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from solders.pubkey import Pubkey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from application.engine import TradingEngine  # noqa: E402
from application.positions import PositionService  # noqa: E402
from monitoring.position_monitor import (  # noqa: E402
    MonitorRetry,
    PositionMonitorManager,
)
from storage.sqlite import SQLitePositionStore  # noqa: E402
from strategies.exit import (  # noqa: E402
    ExitDecisionReason,
    TakeProfitStopLossStrategy,
    TimedExitStrategy,
    TrailingStopStrategy,
)
from trading.universal_trader import UniversalTrader  # noqa: E402


class StrategyTests(unittest.TestCase):
    def test_take_profit_decision_only(self):
        strategy = TakeProfitStopLossStrategy(150, 80)
        result = strategy.evaluate(151)
        self.assertTrue(result.should_exit)
        self.assertEqual(result.reason, ExitDecisionReason.TAKE_PROFIT)

    def test_stop_loss_decision_only(self):
        strategy = TakeProfitStopLossStrategy(150, 80)
        result = strategy.evaluate(79)
        self.assertTrue(result.should_exit)
        self.assertEqual(result.reason, ExitDecisionReason.STOP_LOSS)

    def test_timed_exit(self):
        opened = datetime.now(UTC) - timedelta(seconds=11)
        self.assertTrue(TimedExitStrategy(opened, 10).evaluate().should_exit)

    def test_trailing_stop_tracks_high_water(self):
        strategy = TrailingStopStrategy(1000)
        self.assertFalse(strategy.evaluate(100).should_exit)
        self.assertFalse(strategy.evaluate(200).should_exit)
        result = strategy.evaluate(179)
        self.assertTrue(result.should_exit)
        self.assertEqual(result.reason, ExitDecisionReason.TRAILING_STOP)


class PositionMonitorConcurrencyTests(unittest.TestCase):
    def test_monitors_are_bounded_and_independent(self):
        async def scenario():
            manager = PositionMonitorManager(2)
            await manager.start()
            active = 0
            maximum = 0
            completed = []
            lock = asyncio.Lock()

            def factory(index):
                async def run():
                    nonlocal active, maximum
                    async with lock:
                        active += 1
                        maximum = max(maximum, active)
                    await asyncio.sleep(0.01)
                    completed.append(index)
                    async with lock:
                        active -= 1

                return run

            for index in range(5):
                await manager.submit(str(index), factory(index))
            await manager.join()
            await manager.stop()
            return maximum, completed

        maximum, completed = asyncio.run(scenario())
        self.assertEqual(maximum, 2)
        self.assertEqual(sorted(completed), list(range(5)))

    def test_duplicate_position_monitor_is_rejected(self):
        async def scenario():
            manager = PositionMonitorManager(1)
            await manager.start()
            gate = asyncio.Event()

            async def monitor():
                await gate.wait()

            first = await manager.submit("p", monitor)
            second = await manager.submit("p", monitor)
            gate.set()
            await manager.join()
            await manager.stop()
            return first, second

        self.assertEqual(asyncio.run(scenario()), (True, False))

    def test_retry_is_requeued_without_spawning(self):
        async def scenario():
            manager = PositionMonitorManager(1)
            await manager.start()
            calls = 0

            async def monitor():
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise MonitorRetry(0)

            await manager.submit("p", monitor)
            await manager.join()
            await manager.stop()
            return calls

        self.assertEqual(asyncio.run(scenario()), 2)

    def test_duplicate_token_events_are_claimed_before_dequeue(self):
        async def scenario():
            trader = UniversalTrader.__new__(UniversalTrader)
            trader.processed_tokens = set()
            trader.queued_tokens = set()
            trader.token_timestamps = {}
            trader.token_queue = asyncio.Queue()
            token = SimpleNamespace(
                mint=Pubkey.new_unique(),
                symbol="DUP",
                platform=SimpleNamespace(value="pump_fun"),
            )
            await asyncio.gather(trader._queue_token(token), trader._queue_token(token))
            return trader.token_queue.qsize(), len(trader.queued_tokens)

        self.assertEqual(asyncio.run(scenario()), (1, 1))


class TradingEngineFacadeTests(unittest.TestCase):
    def test_settings_and_position_methods_do_not_require_universal_trader(self):
        class Unused:
            async def buy(self, _request):
                raise AssertionError

            async def sell(self, _request):
                raise AssertionError

            async def get_wallet_balance(self):
                return 123

        with tempfile.TemporaryDirectory() as temp:
            store = SQLitePositionStore(Path(temp) / "state.sqlite3")
            try:
                engine = TradingEngine(
                    Unused(), Unused(), PositionService(store), Unused()
                )
                self.assertEqual(asyncio.run(engine.get_wallet_balance()), 123)
                self.assertEqual(
                    engine.update_settings({"slippage_bps": 500}), {"slippage_bps": 500}
                )
                self.assertEqual(engine.list_positions(), [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
