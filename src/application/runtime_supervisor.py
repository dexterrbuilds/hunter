"""Structured ownership and failure observation for long-running tasks."""

# Supervisor misuse should fail with direct actionable errors.
# ruff: noqa: TRY003

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from application.runtime_models import TaskFailurePolicy

FailureObserver = Callable[[str, TaskFailurePolicy, BaseException], None]


@dataclass(slots=True)
class SupervisedTask:
    name: str
    policy: TaskFailurePolicy
    task: asyncio.Task[None]


class RuntimeTaskSupervisor:
    """Own tasks so crashes and shutdown are never invisible."""

    def __init__(self, failure_observer: FailureObserver) -> None:
        self._failure_observer = failure_observer
        self._tasks: dict[str, SupervisedTask] = {}
        self.accepting_tasks = True

    def create(
        self,
        name: str,
        awaitable: Awaitable[None],
        *,
        policy: TaskFailurePolicy,
    ) -> asyncio.Task[None]:
        if not self.accepting_tasks:
            raise RuntimeError("runtime supervisor is shutting down")
        if name in self._tasks and not self._tasks[name].task.done():
            raise ValueError(f"runtime task already active: {name}")
        task = asyncio.create_task(awaitable, name=name)
        self._tasks[name] = SupervisedTask(name, policy, task)
        task.add_done_callback(lambda completed: self._observe(name, completed))
        return task

    def _observe(self, name: str, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            item = self._tasks[name]
            self._failure_observer(name, item.policy, error)

    def snapshot(self) -> tuple[tuple[str, str, bool], ...]:
        return tuple(
            (name, item.policy.value, not item.task.done())
            for name, item in self._tasks.items()
        )

    async def shutdown(self, timeout_seconds: float = 10.0) -> None:
        self.accepting_tasks = False
        tasks = [item.task for item in self._tasks.values() if not item.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                pass
        self._tasks.clear()
