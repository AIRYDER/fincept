"""api.task_manager — uniform background task lifecycle management.

Extracts the 6+ manually-managed background tasks from the API lifespan
into a single coordinator that handles start, shutdown, and cancellation
uniformly. Reduces the lifespan from ~100 lines of task boilerplate to
a few lines of registration + a single ``await task_manager.shutdown()``.

Usage::

    tm = TaskManager()
    tm.add_task("heartbeat", beat_periodically(redis, "api"))
    tm.add_scheduler("alpaca", AlpacaScheduler(redis))
    await tm.start_all()
    # ... yield ...
    await tm.shutdown()
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from fincept_core.logging import get_logger

log = get_logger(__name__)


class Stoppable(Protocol):
    """Protocol for schedulers that have async start/stop."""

    def start(self) -> None: ...
    async def stop(self) -> None: ...


class TaskManager:
    """Manages background asyncio tasks and schedulers.

    Tasks are cancelled on shutdown; schedulers are stopped.
    Cancellation errors are swallowed (normal shutdown).
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._schedulers: dict[str, Stoppable] = {}
        self._started = False

    def add_task(self, name: str, coro: Any) -> asyncio.Task[Any]:
        """Create and register a background task from a coroutine.

        The task is created immediately (not awaited). If the task raises
        an exception, it is logged but does not crash the manager.
        """
        task = asyncio.create_task(coro, name=name)
        self._tasks[name] = task
        task.add_done_callback(self._on_task_done)
        return task

    def add_scheduler(self, name: str, scheduler: Stoppable) -> Stoppable:
        """Register a scheduler with start/stop methods."""
        self._schedulers[name] = scheduler
        return scheduler

    def start_all(self) -> None:
        """Start all registered schedulers."""
        for name, scheduler in self._schedulers.items():
            scheduler.start()
            log.info("task_manager.scheduler_started", name=name)
        self._started = True

    async def shutdown(self) -> None:
        """Cancel all tasks and stop all schedulers in reverse order.

        Cancellation errors are swallowed (normal shutdown).
        Schedulers are stopped after tasks are cancelled.
        """
        # Cancel tasks in reverse registration order.
        for name in reversed(list(self._tasks.keys())):
            task = self._tasks[name]
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    log.warning(
                        "task_manager.task_error_on_shutdown",
                        name=name,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            log.info("task_manager.task_stopped", name=name)

        # Stop schedulers in reverse order.
        for name in reversed(list(self._schedulers.keys())):
            scheduler = self._schedulers[name]
            try:
                await scheduler.stop()
                log.info("task_manager.scheduler_stopped", name=name)
            except Exception as exc:
                log.warning(
                    "task_manager.scheduler_error_on_shutdown",
                    name=name,
                    error=f"{type(exc).__name__}: {exc}",
                )

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        """Callback for when a task completes (normally or via exception)."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            name = task.get_name()
            log.error(
                "task_manager.task_crashed",
                name=name,
                error=f"{type(exc).__name__}: {exc}",
            )

    @property
    def task_names(self) -> list[str]:
        """Return the names of all registered tasks."""
        return list(self._tasks.keys())

    def get_task(self, name: str) -> asyncio.Task[Any] | None:
        """Return a task by name, or None if not registered."""
        return self._tasks.get(name)
