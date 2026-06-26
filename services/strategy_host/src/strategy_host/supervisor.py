"""
strategy_host.supervisor — reconciles StrategyConfigStore against
running asyncio tasks.

Lifecycle policy
~~~~~~~~~~~~~~~~

Every ``poll_interval_sec`` the supervisor reads the full set of
configs and computes the *desired* state:

  * ``enabled == True``  -> a runner task should be active.
  * ``enabled == False`` -> no runner should be active.

It then reconciles the actual set of running tasks with the desired
set:

  * Desired but not running             -> start.
  * Running but not desired             -> cancel + await.
  * Running, desired, runtime signature
    changed (class_name, symbols,
    params, model_binding)              -> cancel + await + start.

Why a *runtime signature* and not full equality?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Every upsert bumps ``updated_at`` (the store's design — see
``fincept_core.strategy_config.upsert``).  A pure equality check
would restart the runner on every upsert, even a redundant one.
Restarts are expensive (the strategy loses its in-memory state and
warm-up windows) so we only restart on changes to fields the runner
actually depends on.

What about ``enabled``?
~~~~~~~~~~~~~~~~~~~~~~~

``enabled`` is the start/stop axis, not a runtime property.  Flipping
it does not trigger a restart of an already-stopped or already-
running runner -- it triggers a different control-flow path (start
or cancel).

Crash semantics
~~~~~~~~~~~~~~~

If a runner crashes (raises out of the task), the supervisor logs the
exception and starts it again on the next poll tick.  Because runners
are pure state machines parameterised on the config, a fresh start
is always safe -- positions live in the portfolio service and bar
windows reseed from the live stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from redis.asyncio import Redis

from fincept_core.strategy_config import (
    StrategyConfig,
    StrategyConfigStore,
)

logger = logging.getLogger(__name__)


# Type of the per-strategy runner entrypoint the supervisor calls.
#
# The supervisor passes ``(config, redis, stop)`` and expects the
# runner to return when ``stop`` is set or it raises CancelledError.
# F3 will replace ``run_strategy`` below with the real dispatcher;
# the type stays the same so the supervisor never has to change.
RunnerFn = Callable[
    [StrategyConfig, "Redis[Any]", asyncio.Event],
    Coroutine[Any, Any, None],
]


# --------------------------------------------------------------------------- #
# Runtime signature: what triggers a restart                                  #
# --------------------------------------------------------------------------- #


def _runtime_signature(cfg: StrategyConfig) -> str:
    """Return a stable string capturing the runtime-relevant fields.

    Two configs with the same signature can keep running without a
    restart; a signature change forces cancel-and-respawn.

    ``enabled`` is intentionally excluded (it controls
    start/stop, not restart) and so are the timestamps (they bump on
    every upsert; including them would defeat the whole point of
    the signature).
    """
    return json.dumps(
        {
            "class_name": cfg.class_name,
            "symbols": list(cfg.symbols),
            # ``params`` may contain unhashable types; JSON with
            # sorted keys gives us a deterministic comparable form.
            "params": cfg.params,
            "model_binding": cfg.model_binding,
        },
        sort_keys=True,
    )


# --------------------------------------------------------------------------- #
# Supervisor                                                                  #
# --------------------------------------------------------------------------- #


class Supervisor:
    """Per-host reconciler between StrategyConfigStore and running tasks.

    Construct once per host process; call :meth:`run` from the host's
    main coroutine.  Tests can drive the loop manually via :meth:`tick`
    without spawning the polling task.
    """

    def __init__(
        self,
        *,
        store: StrategyConfigStore,
        redis: Redis[Any],
        runner: RunnerFn,
        poll_interval_sec: float = 2.0,
    ) -> None:
        self._store = store
        self._redis = redis
        self._runner = runner
        self._poll_interval_sec = float(poll_interval_sec)
        # Per-strategy state: the live task plus the signature it
        # was started with so we can detect changes on the next tick.
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._signatures: dict[str, str] = {}
        # Per-strategy stop event we pass into the runner.  We
        # *also* hold an external task-cancel as the primary
        # shutdown path; the event is for cooperative shutdown
        # in the runner.
        self._stops: dict[str, asyncio.Event] = {}

    # ------ introspection ------------------------------------------- #

    @property
    def running(self) -> set[str]:
        """Strategy IDs currently active.  For tests and ops debugging."""
        return set(self._tasks)

    # ------ control loop -------------------------------------------- #

    async def run(self, stop: asyncio.Event) -> None:
        """Polling reconciler.  Returns when ``stop`` is set."""
        try:
            while not stop.is_set():
                try:
                    await self.tick()
                except Exception:
                    # A bad config or transient store error must NOT
                    # take the supervisor down -- log and try again
                    # next tick.  The dashboard will show the stale
                    # state until the operator fixes the underlying
                    # issue.
                    logger.exception("supervisor.tick_failed")
                # Sleep with cancellation responsiveness: wait_for on
                # ``stop`` returns early on shutdown rather than
                # sleeping the full interval.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_sec)
        finally:
            await self._cancel_all()

    async def tick(self) -> None:
        """Run one reconcile pass.  Public so tests can call it directly.

        Order matters: we reap dead tasks BEFORE reconciling.  If a
        runner crashed since the last tick, the dead task is still
        registered in ``self._tasks``; reaping first removes it so
        the reconcile loop sees "alpha is desired but not running"
        and re-spawns it.  Reaping after reconcile would cause the
        dead task to be treated as already-running and the desired
        restart would never fire until the next tick at the earliest.
        """
        await self._reap_dead()
        configs = self._store.list_all()
        desired: dict[str, StrategyConfig] = {c.strategy_id: c for c in configs if c.enabled}
        await self._reconcile(desired)

    # ------ reconcile ----------------------------------------------- #

    async def _reconcile(self, desired: dict[str, StrategyConfig]) -> None:
        # Phase 1: cancel runners that are no longer desired or whose
        # signature changed.
        for sid in list(self._tasks):
            if sid not in desired:
                logger.info(
                    "supervisor.stop_runner strategy_id=%s reason=disabled",
                    sid,
                )
                await self._stop_runner(sid)
                continue
            new_sig = _runtime_signature(desired[sid])
            if new_sig != self._signatures.get(sid):
                logger.info(
                    "supervisor.restart_runner strategy_id=%s reason=config_changed",
                    sid,
                )
                await self._stop_runner(sid)
                # Fall through to phase 2 to start it again with the
                # new config.
        # Phase 2: start runners that are desired but not running.
        for sid, cfg in desired.items():
            if sid in self._tasks:
                continue
            self._start_runner(cfg)

    def _start_runner(self, config: StrategyConfig) -> None:
        stop = asyncio.Event()
        task: asyncio.Task[None] = asyncio.create_task(
            self._runner(config, self._redis, stop),
            name=f"strategy:{config.strategy_id}",
        )
        self._tasks[config.strategy_id] = task
        self._stops[config.strategy_id] = stop
        self._signatures[config.strategy_id] = _runtime_signature(config)
        logger.info(
            "supervisor.start_runner strategy_id=%s class_name=%s",
            config.strategy_id,
            config.class_name,
        )

    async def _stop_runner(self, strategy_id: str) -> None:
        task = self._tasks.pop(strategy_id, None)
        stop = self._stops.pop(strategy_id, None)
        self._signatures.pop(strategy_id, None)
        if task is None:
            return
        if stop is not None:
            stop.set()
        # Cancel as the primary shutdown path so a runner that
        # ignores ``stop`` still terminates within one tick.
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # Expected: cancellation propagated cleanly.
            pass
        except Exception as exc:
            # Belt-and-suspenders: a runner that already crashed
            # before we got to cancel it would re-raise its
            # exception here.  ``_reap_dead`` (or this very call,
            # if reap hasn't yet run) is the canonical place to log
            # crashes; we just don't want a stale error to break
            # the supervisor's shutdown sequence.
            logger.debug(
                "supervisor.stop_runner_swallowed strategy_id=%s err=%r",
                strategy_id,
                exc,
            )

    async def _cancel_all(self) -> None:
        """Cancel every running runner; called from ``run`` finally."""
        for sid in list(self._tasks):
            await self._stop_runner(sid)

    async def _reap_dead(self) -> None:
        """Drop tasks that have ended without supervisor intervention.

        A runner that raised an exception or returned early would
        otherwise stay in ``self._tasks`` forever and prevent a
        restart.  We log the failure here so the operator sees it
        in the host's stderr stream.
        """
        for sid in list(self._tasks):
            task = self._tasks[sid]
            if not task.done():
                continue
            exc = task.exception()
            if exc is not None:
                logger.warning(
                    "supervisor.runner_crashed strategy_id=%s error=%r",
                    sid,
                    exc,
                )
            else:
                logger.info("supervisor.runner_exited strategy_id=%s", sid)
            self._tasks.pop(sid, None)
            self._stops.pop(sid, None)
            self._signatures.pop(sid, None)
