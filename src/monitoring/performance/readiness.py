"""Required/optional dependency readiness and explicit degraded operation."""

# Duplicate component names are configuration errors with actionable names.
# ruff: noqa: TRY003

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum


class ComponentState(StrEnum):
    """One dependency's startup state."""

    NOT_READY = "not_ready"
    READY = "ready"
    WARM = "warm"
    DEGRADED = "degraded"


class HunterReadiness(StrEnum):
    """Aggregate state exposed before detection can initiate trading."""

    NOT_READY = "not_ready"
    DEGRADED = "degraded"
    MAXIMUM_PERFORMANCE = "maximum_performance"


@dataclass(slots=True)
class ReadinessComponent:
    """Named dependency plus required/optional policy and safe reason text."""

    name: str
    required: bool
    state: ComponentState = ComponentState.NOT_READY
    reason: str | None = None


Initializer = Callable[[], Awaitable[bool]]


class ReadinessSupervisor:
    """Initialize dependencies concurrently and enforce fail-closed readiness."""

    def __init__(self, *, allow_degraded: bool) -> None:
        self.allow_degraded = allow_degraded
        self.components: dict[str, ReadinessComponent] = {}

    def register(self, name: str, *, required: bool) -> None:
        if name in self.components:
            raise ValueError(f"readiness component already exists: {name}")
        self.components[name] = ReadinessComponent(name, required)

    def update(
        self, name: str, state: ComponentState, reason: str | None = None
    ) -> None:
        component = self.components[name]
        component.state = state
        component.reason = reason

    @property
    def state(self) -> HunterReadiness:
        required_failed = any(
            item.required
            and item.state not in {ComponentState.READY, ComponentState.WARM}
            for item in self.components.values()
        )
        if required_failed:
            return (
                HunterReadiness.DEGRADED
                if self.allow_degraded
                else HunterReadiness.NOT_READY
            )
        optional_failed = any(
            not item.required
            and item.state not in {ComponentState.READY, ComponentState.WARM}
            for item in self.components.values()
        )
        return (
            HunterReadiness.DEGRADED
            if optional_failed
            else HunterReadiness.MAXIMUM_PERFORMANCE
        )

    @property
    def may_trade(self) -> bool:
        return self.state == HunterReadiness.MAXIMUM_PERFORMANCE or (
            self.state == HunterReadiness.DEGRADED and self.allow_degraded
        )

    async def initialize(self, initializers: dict[str, Initializer]) -> HunterReadiness:
        async def run(name: str, initializer: Initializer) -> None:
            try:
                ready = await initializer()
            except Exception as error:  # noqa: BLE001 - reduced to safe class name
                self.update(name, ComponentState.DEGRADED, type(error).__name__)
                return
            self.update(
                name,
                ComponentState.READY if ready else ComponentState.DEGRADED,
                None if ready else "initialization returned false",
            )

        await asyncio.gather(*(run(name, item) for name, item in initializers.items()))
        return self.state

    def report(self) -> tuple[tuple[str, str, bool, str | None], ...]:
        return tuple(
            (item.name, item.state.value, item.required, item.reason)
            for item in self.components.values()
        )
