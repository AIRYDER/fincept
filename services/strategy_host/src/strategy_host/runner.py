"""
strategy_host.runner — per-strategy live runtime.

One ``run_strategy`` task per enabled :class:`StrategyConfig`.  The
supervisor owns the task lifecycle; this module owns *what happens
inside the task*:

  1. Build the Strategy instance from the registry.
  2. Construct a :class:`LiveStrategyContext` and call ``on_start``.
  3. Loop: tail bars / fills / positions -> dispatch hooks ->
     drain submitted OrderIntents -> publish to ``ord.orders``.
  4. On cancellation: call ``on_stop`` and exit.

Stream wiring
~~~~~~~~~~~~~

  Read   ``md.bars.1m``      -> ``Strategy.on_bar``  (filtered by symbol)
  Read   ``ord.fills``       -> ``Strategy.on_fill`` (filtered by
                                outstanding-order ledger; see below)
  Read   ``ord.positions``   -> update ``ctx.positions`` (filtered
                                by ``Position.strategy_id``)
  Write  ``ord.orders``      <- whatever ``ctx.submit`` queued during
                                a hook

Why this filters fills via a local outstanding-order ledger
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``Fill`` schema doesn't carry ``strategy_id`` -- only
``order_id``.  ``services/portfolio/main.py`` recovers it via the
OMS audit log; we'd rather not pull a ``fincept-db`` dependency
into the strategy host just for that.

The host *already knows* which orders it submitted (it stamped
``strategy_id`` on every ``OrderIntent`` and just published them).
So each runner keeps an in-memory dict ``order_id -> OrderIntent``;
when a Fill arrives, it dispatches to the matching strategy if and
only if the ``order_id`` is in that ledger.  A fill from an order
the host didn't submit (a previous host instance, an out-of-band
manual order) is ignored.

This trades one form of audit dependency (Postgres audit log) for
another (in-memory ledger that doesn't survive restarts).  The
loss-on-restart case is acceptable because:

  * The host process is a single leader; a restart is rare.
  * On restart, the strategy starts with empty pending counters and
    a fresh outstanding ledger.  Any fills that were in flight will
    arrive and be ignored, but ``ctx.positions`` is rebuilt from
    the ``ord.positions`` stream so quantity correctness is
    preserved.
  * No double-submission: the host doesn't reissue lost fills.

Consumer group naming
~~~~~~~~~~~~~~~~~~~~~

Each strategy gets its own group (``strategy_host:<strategy_id>``)
so they don't share message offsets.  This means three strategies
running on overlapping symbol sets all see every bar -- the duplicate
read overhead is small because each consumer just filters and acks.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
from typing import Any

from redis.asyncio import Redis

from backtester.runner import build_strategy
from fincept_bus.consumer import Consumer
from fincept_bus.producer import Producer
from fincept_bus.streams import (
    STREAM_FILLS,
    STREAM_MD_BARS_1M,
    STREAM_POSITIONS,
)
from fincept_core.events import Event
from fincept_core.logging import get_logger
from fincept_core.schemas import (
    BarEvent,
    Fill,
    OrderIntent,
    Position,
)
from fincept_core.strategy_config import StrategyConfig
from strategy_host.model_resolver import resolve_active_model_dir
from strategy_host.outstanding_store import OutstandingOrderStore
from strategy_host.runtime import LiveStrategyContext

log = get_logger(__name__)

# Stream the host writes OrderIntents to.  Mirrors the orchestrator's
# choice in ``services/orchestrator/src/orchestrator/router.py`` --
# the OMS picks intents off this stream and routes them.
STREAM_OUTGOING_ORDERS = "ord.orders"

# Default polling interval for the model-binding watcher.  30s
# matches the agent's default and keeps the FS load low (one stat
# per binding per cycle); responsive enough that an operator who
# clicks Promote sees their change land within ~30s.  Override per
# host via the env var; tests override via the parameter on
# ``run_strategy``.
DEFAULT_RELOAD_POLL_S = 30.0


# --------------------------------------------------------------------------- #
# Hook dispatch                                                              #
# --------------------------------------------------------------------------- #


def _hook_failed(ctx_log: Any, hook: str, strategy_id: str, exc: BaseException) -> None:
    """Log a hook failure without taking the runner down.

    A buggy strategy must not stop the host from running other
    strategies.  We swallow the exception, log at WARNING, and let
    the next event flow normally.
    """
    ctx_log.warning(
        "strategy.hook_failed",
        strategy_id=strategy_id,
        hook=hook,
        error=repr(exc),
    )


async def _publish_pending(
    ctx: LiveStrategyContext,
    producer: Producer,
    outstanding: dict[str, OrderIntent],
    outstanding_store: OutstandingOrderStore | None,
    bound_log: Any,
) -> None:
    """Drain ``ctx``'s submit queue and publish each intent.

    Records each intent in ``outstanding`` so a subsequent fill on
    that ``order_id`` can be attributed back to this strategy.  We
    publish in submission order so partial-fill state machines
    (GBMStrategy._pending_buys) line up with the OMS's own ordering.

    If ``outstanding_store`` is provided, each intent is also persisted
    to Redis so the ledger survives restarts.
    """
    pending = ctx.drain_submits()
    if not pending:
        return
    for intent in pending:
        outstanding[intent.order_id] = intent
        if outstanding_store is not None:
            await outstanding_store.put(intent.order_id, intent)
        try:
            await producer.publish(
                STREAM_OUTGOING_ORDERS,
                Event(type="order_intent", payload=intent),
            )
        except Exception as exc:
            # Publishing failure is rare but possible (Redis hiccup);
            # log and DON'T re-raise so the rest of the strategy's
            # hook output still gets attempted.  The lost intent is
            # the same outcome as a host crash mid-publish, which the
            # design already accepts.
            bound_log.warning(
                "strategy.publish_failed",
                order_id=intent.order_id,
                error=repr(exc),
            )
            continue
        bound_log.info(
            "strategy.order_submitted",
            order_id=intent.order_id,
            symbol=intent.symbol,
            side=intent.side.value,
            quantity=str(intent.quantity),
        )


# --------------------------------------------------------------------------- #
# Per-event handler                                                           #
# --------------------------------------------------------------------------- #


def _make_event_handler(
    *,
    config: StrategyConfig,
    strategy: Any,
    ctx: LiveStrategyContext,
    producer: Producer,
    outstanding: dict[str, OrderIntent],
    outstanding_store: OutstandingOrderStore | None,
    bound_log: Any,
    symbols: set[str],
) -> Any:
    """Closure: one async handler routes every consumed event.

    Holds references to the live strategy + ctx + producer so the
    Consumer's generic dispatch can route to the right code path
    based on payload type alone.  The dict (`outstanding`) is shared
    in-place so fill attribution works across consecutive events.
    """

    async def handle(event: Event) -> None:
        payload = event.payload
        if isinstance(payload, BarEvent):
            if payload.symbol not in symbols:
                return
            ctx.now_ns = payload.ts_event
            try:
                strategy.on_bar(ctx, payload)
            except Exception as exc:
                _hook_failed(bound_log, "on_bar", config.strategy_id, exc)
            await _publish_pending(ctx, producer, outstanding, outstanding_store, bound_log)
            return
        if isinstance(payload, Position):
            # Filter to this strategy.  ``update_position`` also
            # double-checks (defensive); both checks are cheap.
            if payload.strategy_id != config.strategy_id:
                return
            ctx.update_position(payload)
            return
        if isinstance(payload, Fill):
            intent = outstanding.get(payload.order_id)
            if intent is None or intent.strategy_id != config.strategy_id:
                # Either not our order, or a fill we never tracked
                # (e.g., a fill that arrived before we started).
                return
            ctx.now_ns = payload.ts_event
            try:
                strategy.on_fill(ctx, payload)
            except Exception as exc:
                _hook_failed(bound_log, "on_fill", config.strategy_id, exc)
            # The strategy may submit follow-up orders inside on_fill
            # (e.g., a stop-flat after a fill arrives); drain those.
            await _publish_pending(ctx, producer, outstanding, outstanding_store, bound_log)
            # We deliberately leave the OrderIntent in ``outstanding``
            # because partial fills mean more Fill events for the
            # same ``order_id`` are still possible.  A future cleanup
            # task can LRU-evict by age; for F3 unbounded growth is
            # bounded in practice by host restarts.
            return
        # Other event types (predictions, sentiment, etc.) aren't
        # routed in F3.  A future strategy class that needs them
        # would be subscribed via on_signal; for now those events
        # don't even land on this consumer because we only read the
        # three streams we care about.

    return handle


# --------------------------------------------------------------------------- #
# Public entrypoint                                                           #
# --------------------------------------------------------------------------- #


async def _watch_model_binding(
    *,
    binding: str,
    strategy: Any,
    initial_dir: pathlib.Path,
    stop: asyncio.Event,
    bound_log: Any,
    poll_interval_s: float,
) -> None:
    """Poll the binding pointer; reload the strategy when it changes.

    Runs as a sibling task to the consumer.  Both are on the same
    event loop, so the synchronous ``strategy.reload_from_dir`` call
    can never interleave with a synchronous ``strategy.on_bar``
    call -- asyncio yields only at ``await`` boundaries, and neither
    hook awaits inside its body.

    Failure handling mirrors the agent's hot-reload: a failed reload
    logs and continues.  The strategy keeps using its currently-
    loaded model.  This means an operator who promotes a corrupt
    model sees a clear log entry but doesn't take down the live
    strategy.

    Exits when ``stop`` is set.  The runner's ``finally`` block also
    cancels this task as belt-and-suspenders.
    """
    current_dir = initial_dir
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=poll_interval_s)
        if stop.is_set():
            return
        new_dir = resolve_active_model_dir(binding)
        if new_dir is None:
            # Pointer disappeared or became malformed since last
            # poll.  ``resolve_active_model_dir`` already logged a
            # warning; we just keep the current model loaded.
            continue
        if new_dir == current_dir:
            continue
        try:
            strategy.reload_from_dir(new_dir)
        except Exception as exc:
            # Atomic reload guarantees the old model is still in
            # place.  Log loud enough that a tail -f catches it but
            # don't crash the runner.
            bound_log.warning(
                "strategy.reload_failed",
                model_binding=binding,
                from_dir=str(current_dir),
                to_dir=str(new_dir),
                error=repr(exc),
            )
            continue
        bound_log.info(
            "strategy.reloaded",
            model_binding=binding,
            from_dir=str(current_dir),
            to_dir=str(new_dir),
        )
        current_dir = new_dir


async def run_strategy(
    config: StrategyConfig,
    redis: Redis[Any],
    stop: asyncio.Event,
    *,
    reload_poll_interval_s: float | None = None,
) -> None:
    """Live runner for one strategy instance.

    Returns when:
      * ``stop`` is set (cooperative shutdown), or
      * The task is cancelled (supervisor-driven restart / shutdown).

    Errors at construct time (unknown class, bad params, unresolvable
    model binding) are logged and the runner exits silently.  The
    supervisor will see the crash via ``_reap_dead`` and either
    restart it (on the next config tick) or leave it cancelled (if
    ``enabled`` flipped off).

    Model-binding behaviour
    ~~~~~~~~~~~~~~~~~~~~~~~

    If ``config.model_binding`` is set, the runner resolves it to a
    path via ``models/active/<binding>.json`` and injects
    ``model_dir=<path>`` into ``params`` before constructing the
    strategy.  This mirrors how the backtester's
    ``backtester.runner.build_strategy`` consumes ``model_dir``.

    After ``on_start``, if the strategy class implements
    ``reload_from_dir`` (duck-typed), a watcher task is spawned that
    polls the pointer every ``reload_poll_interval_s`` seconds and
    swaps the model in place when the pointer changes.  Strategy
    classes without ``reload_from_dir`` (BuyAndHold, MovingAverage
    Crossover) silently skip the watcher: they don't use models so
    a model-binding change is meaningless to them.
    """
    bound_log = log.bind(strategy_id=config.strategy_id)
    bound_log.info(
        "strategy.start",
        class_name=config.class_name,
        symbols=config.symbols,
        model_binding=config.model_binding,
    )

    if reload_poll_interval_s is None:
        reload_poll_interval_s = float(
            os.environ.get("STRATEGY_HOST_RELOAD_POLL_S", DEFAULT_RELOAD_POLL_S)
        )

    # ---- resolve model binding (if any) and inject into params ----
    params = dict(config.params)
    initial_model_dir: pathlib.Path | None = None
    if config.model_binding:
        initial_model_dir = resolve_active_model_dir(config.model_binding)
        if initial_model_dir is None:
            # Conservative: refuse to start.  An unresolvable
            # binding would either cold-load the wrong model (very
            # bad) or fall through to a default the operator didn't
            # ask for (also bad).  Better to log and exit; the
            # supervisor will see the task complete and try again
            # on the next tick if the operator fixes the pointer.
            bound_log.error(
                "strategy.binding_unresolvable",
                model_binding=config.model_binding,
            )
            return
        # Inject AFTER copying params so we don't mutate the
        # operator's stored config.  The strategy class is free to
        # ignore ``model_dir`` if it doesn't take one (BuyAndHold);
        # passing it as kwargs would TypeError, so only inject when
        # the strategy is known to accept it.  Heuristic: any
        # strategy that supports ``reload_from_dir`` accepts
        # ``model_dir``.  This is a reasonable contract -- if you
        # can hot-reload from a directory, you must have known what
        # directory you started from.  We can't check the heuristic
        # before the strategy is built, though, so we always inject
        # when a binding is set and rely on ``build_strategy`` to
        # surface a TypeError if the class doesn't accept it.
        params["model_dir"] = str(initial_model_dir)
        bound_log.info(
            "strategy.binding_resolved",
            model_binding=config.model_binding,
            model_dir=str(initial_model_dir),
        )

    # ---- build the strategy instance ----
    try:
        strategy = build_strategy(
            config.class_name,
            symbols=config.symbols,
            params=params,
        )
    except Exception as exc:
        bound_log.error("strategy.build_failed", error=repr(exc))
        return

    ctx = LiveStrategyContext(strategy_id=config.strategy_id, log=bound_log)
    producer = Producer(redis)
    consumer = Consumer(redis)
    outstanding_store = OutstandingOrderStore(redis, config.strategy_id)
    # Hydrate the outstanding-order ledger from Redis so fills for
    # orders submitted before a restart are still attributed correctly.
    outstanding = await outstanding_store.hydrate()
    symbols = set(config.symbols)

    # ---- on_start ----
    try:
        strategy.on_start(ctx)
    except Exception as exc:
        bound_log.error("strategy.on_start_failed", error=repr(exc))
        return

    # ---- consume loop ----
    handler = _make_event_handler(
        config=config,
        strategy=strategy,
        ctx=ctx,
        producer=producer,
        outstanding=outstanding,
        outstanding_store=outstanding_store,
        bound_log=bound_log,
        symbols=symbols,
    )
    consumer_name = f"strategy_host:{config.strategy_id}"
    group = consumer_name
    consume_task = asyncio.create_task(
        consumer.consume(
            streams=[STREAM_MD_BARS_1M, STREAM_FILLS, STREAM_POSITIONS],
            group=group,
            consumer_name=consumer_name,
            handler=handler,
        ),
        name=f"strategy_host:consumer:{config.strategy_id}",
    )

    # ---- watcher task (only if the strategy supports hot-reload) ----
    watcher_task: asyncio.Task[None] | None = None
    if (
        config.model_binding
        and initial_model_dir is not None
        and hasattr(strategy, "reload_from_dir")
    ):
        watcher_task = asyncio.create_task(
            _watch_model_binding(
                binding=config.model_binding,
                strategy=strategy,
                initial_dir=initial_model_dir,
                stop=stop,
                bound_log=bound_log,
                poll_interval_s=reload_poll_interval_s,
            ),
            name=f"strategy_host:watcher:{config.strategy_id}",
        )

    try:
        # Wait for either supervisor-driven cancel (CancelledError
        # raised here) or cooperative stop.set().
        await stop.wait()
    finally:
        # Cancel both background tasks.  Order doesn't matter; both
        # honour cancellation cleanly and we await each in turn.
        for t in (consume_task, watcher_task):
            if t is None:
                continue
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        # ---- on_stop ----  Always run, even if ``on_start`` was
        # the last successful hook -- gives the strategy a chance
        # to flush state.
        try:
            strategy.on_stop(ctx)
        except Exception as exc:
            bound_log.warning("strategy.on_stop_failed", error=repr(exc))
        bound_log.info("strategy.stop")
