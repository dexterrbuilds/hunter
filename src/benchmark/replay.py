"""Offline replay primitives that never submit a transaction."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic_ns
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    """Benchmark safety switch; live submission is denied by default."""

    allow_live_submission: bool = False

    def require_live_opt_in(self) -> None:
        if not self.allow_live_submission:
            raise PermissionError(  # noqa: TRY003
                "live benchmark submission is disabled; set "
                "benchmark.allow_live_submission=true explicitly"
            )


@dataclass(frozen=True, slots=True)
class BenchmarkSample(Generic[R]):
    """One monotonic benchmark result."""

    elapsed_ms: float
    result: R


class OfflineReplayBenchmark(Generic[T, R]):
    """Replay recorded detector inputs through a pure/offline callback."""

    def __init__(self, operation: Callable[[T], Awaitable[R] | R]) -> None:
        self.operation = operation

    async def run(self, events: Iterable[T]) -> list[BenchmarkSample[R]]:
        samples: list[BenchmarkSample[R]] = []
        for event in events:
            started = monotonic_ns()
            value = self.operation(event)
            if asyncio.iscoroutine(value):
                value = await value
            elapsed = (monotonic_ns() - started) / 1_000_000
            samples.append(BenchmarkSample(elapsed, value))
        return samples


def benchmark_sync(operation: Callable[[], R], iterations: int) -> list[float]:
    """Measure construction/signing callables without network activity."""
    if iterations <= 0:
        raise ValueError("benchmark iterations must be positive")  # noqa: TRY003
    samples: list[float] = []
    for _ in range(iterations):
        started = monotonic_ns()
        operation()
        samples.append((monotonic_ns() - started) / 1_000_000)
    return samples
