"""
agents.gbm_predictor.main - long-running entrypoint.

  python -m agents.gbm_predictor.main

Resolves the model directory in this order:

  1. ``models/active/gbm_predictor.v1.json``  (operator promotion)
  2. ``$GBM_MODEL_DIR``                        (env override)
  3. ``models/gbm_predictor``                  (default)

then loads the trained Booster and publishes :class:`Prediction`
events to ``STREAM_SIG_PREDICT`` at a fixed cadence.

The active.json pointer is written by the api's ``POST /models/{name}/promote``
route.  Operators who prefer not to use the dashboard can ignore it
and keep using ``GBM_MODEL_DIR`` -- the resolver falls through cleanly
when the file is absent or malformed.

**Hot-reload (Phase D1):** the run loop re-resolves the active pointer
every ``GBM_RELOAD_POLL_S`` seconds (default 30).  When the resolved
model directory changes, the agent loads the new booster + meta into
a fresh :class:`GBMPredictor`, atomically replaces the running publish
task, and tears down the old agent.  A failed load leaves the previous
booster running -- a corrupted active.json or a deleted model directory
will *not* take the agent down.  This is what makes the dashboard's
"Promote" button take effect without a manual service restart.

Graceful shutdown on SIGINT / SIGTERM cancels the run loop so
``teardown`` runs and the Redis connection closes cleanly.

If the model artifacts are missing on initial startup, the process
exits non-zero with a clear error.  Run the trainer first:

  python -m agents.gbm_predictor.train --input <bars.parquet>
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pathlib
import signal
from typing import Any

from redis.asyncio import Redis

from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_SIG_PREDICT
from fincept_core.config import get_settings
from fincept_core.events import Event
from fincept_core.heartbeat import beat_periodically
from fincept_core.logging import configure_logging, get_logger
from fincept_core.schemas import Prediction
from fincept_core.tracing import configure_tracing

from fincept_core.prediction_log import PredictionLog

from agents.gbm_predictor.infer import GBMPredictor

log = get_logger(__name__)

AGENT_ID = "gbm_predictor.v1"
DEFAULT_MODEL_DIR = "models/gbm_predictor"

# Poll interval for the active.json watcher.  30s is friendly to the
# filesystem (one stat per cycle), responsive enough for an operator
# clicking Promote (worst case ~30s to take effect), and easy to crank
# down from tests via the env var without monkey-patching.
DEFAULT_RELOAD_POLL_S = 30.0


def _resolve_model_dir() -> pathlib.Path:
    """Pick the model directory the agent should load on startup.

    Three-tier resolution (see module docstring).  Duplicated here
    rather than imported from ``api.promotions`` because the agent
    process must not depend on the api package -- the api ships
    fastapi/pydantic and a long tail of HTTP-side baggage that the
    agent doesn't need.

    The function is intentionally fail-soft: a corrupted active.json
    is logged and skipped.  The agent would rather fall through to
    the env-var path than refuse to start, since a model-loading
    failure later will produce a clearer error in setup().
    """
    models_root = pathlib.Path(os.environ.get("MODELS_DIR", "models"))
    active_dir_override = os.environ.get("ACTIVE_MODELS_DIR")
    active_dir = (
        pathlib.Path(active_dir_override)
        if active_dir_override
        else models_root / "active"
    )
    pointer = active_dir / f"{AGENT_ID}.json"
    if pointer.is_file():
        try:
            data = json.loads(pointer.read_text())
            name = data.get("model_name")
            if name and isinstance(name, str):
                resolved = models_root / name
                log.info("gbm.model.from_active", pointer=str(pointer), model=name)
                return resolved
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            # Don't fail the agent -- fall through to env / default.
            logging.getLogger(__name__).warning(
                "gbm.model.active_ignored: %s (%s)", pointer, exc
            )
    env_override = os.environ.get("GBM_MODEL_DIR")
    if env_override:
        return pathlib.Path(env_override)
    return models_root / "gbm_predictor"


async def _build_agent(
    model_dir: pathlib.Path, redis: Redis[Any]
) -> GBMPredictor:
    """Construct + setup() a GBMPredictor for ``model_dir``.

    Centralised so the initial load and every hot-reload go through
    the same path; tests can monkey-patch this to inject a fake.
    Raises whatever ``GBMPredictor.setup`` raises (typically
    ``FileNotFoundError`` when the model artifacts are missing).
    """
    agent = GBMPredictor(model_dir=model_dir, redis=redis)
    await agent.setup()
    return agent


async def _wait_or_timeout(stop: asyncio.Event, timeout_s: float) -> bool:
    """Sleep up to ``timeout_s`` or until ``stop`` is set.

    Returns ``True`` if the stop event fired (caller should exit),
    ``False`` if the timeout elapsed (caller should poll).  Centralised
    so the run loop reads cleanly: ``if await _wait_or_timeout(...)``.
    """
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout_s)
    except (asyncio.TimeoutError, TimeoutError):
        return False
    return True


async def run(
    stop: asyncio.Event,
    *,
    redis: Redis[Any] | None = None,
    poll_interval_s: float | None = None,
    build_agent: Any = None,
    publish_loop: Any = None,
    heartbeat: Any = None,
) -> None:
    """Long-running entrypoint: load model, publish, hot-reload.

    Production callers pass only ``stop``; everything else has a
    default that wires up the real Redis client, the real LightGBM
    agent loader, and the real publish + heartbeat loops.  The keyword
    overrides exist so tests can inject fakes without monkey-patching
    module-level symbols (which is brittle when the symbol is a class
    imported into the module's namespace).

    The control flow is:

      1. Resolve the active model dir, build & setup() the agent.
      2. Spawn the publish task (yields predictions until cancelled).
      3. Loop: sleep for the reload poll interval, re-resolve.  If the
         pointer now points elsewhere, build the new agent in the
         background -- on success, swap it in atomically; on failure,
         keep the current agent running.
      4. On stop: cancel publish + heartbeat tasks, teardown the
         current agent, close Redis.

    This shape keeps the failure mode strict on cold-start (a missing
    model is a hard exit) while making in-flight reloads forgiving (a
    bad pointer becomes a logged warning, not an outage).
    """
    settings = get_settings()
    if redis is None:
        redis = Redis.from_url(settings.REDIS_URL)
    if poll_interval_s is None:
        poll_interval_s = float(
            os.environ.get("GBM_RELOAD_POLL_S", DEFAULT_RELOAD_POLL_S)
        )
    if build_agent is None:
        build_agent = _build_agent
    if publish_loop is None:
        publish_loop = _publish_loop
    if heartbeat is None:
        heartbeat = beat_periodically

    producer = Producer(redis)
    # The prediction log is shared across reloads -- it's keyed by
    # (agent_id, model_name) inside, so each appended row carries the
    # name of the booster that emitted it even after a hot-reload.
    prediction_log = PredictionLog()

    current_dir = _resolve_model_dir()
    log.info(
        "gbm.start",
        model_dir=str(current_dir),
        universe=list(settings.UNIVERSE),
        reload_poll_s=poll_interval_s,
    )
    # Initial load: a failure here SHOULD take the process down so the
    # operator notices and runs the trainer.
    current_agent = await build_agent(current_dir, redis)
    publish_task = asyncio.create_task(
        publish_loop(
            current_agent,
            producer,
            prediction_log=prediction_log,
            model_name=current_dir.name,
        )
    )
    heartbeat_task = asyncio.create_task(heartbeat(redis, "gbm_predictor"))

    try:
        while True:
            stopped = await _wait_or_timeout(stop, poll_interval_s)
            if stopped:
                break

            new_dir = _resolve_model_dir()
            if new_dir == current_dir:
                continue

            # Build the *new* agent before tearing down the old one --
            # if setup() raises, we keep serving predictions from the
            # currently-loaded model and just log the failure.
            try:
                new_agent = await build_agent(new_dir, redis)
            except (FileNotFoundError, OSError) as exc:
                log.warning(
                    "gbm.reload_failed",
                    new_dir=str(new_dir),
                    error=str(exc),
                )
                continue

            log.info(
                "gbm.reload",
                from_dir=str(current_dir),
                to_dir=str(new_dir),
            )
            publish_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await publish_task
            await current_agent.teardown()

            current_agent = new_agent
            current_dir = new_dir
            publish_task = asyncio.create_task(
                publish_loop(
                    current_agent,
                    producer,
                    prediction_log=prediction_log,
                    model_name=current_dir.name,
                )
            )
    finally:
        for task in (heartbeat_task, publish_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await current_agent.teardown()
        await redis.aclose()  # type: ignore[attr-defined]


async def _publish_loop(
    agent: GBMPredictor,
    producer: Producer,
    *,
    prediction_log: PredictionLog | None = None,
    model_name: str | None = None,
) -> None:
    """Publish predictions to Redis and (optionally) record to disk.

    The two side effects are independent: a Redis publish failure
    must NOT prevent the disk record (and vice-versa), but currently
    we don't have a real failure scenario for either, so we let the
    natural error-propagation path take over.  If/when this becomes
    a concern, we'll add try/except around each side effect.

    ``prediction_log`` and ``model_name`` are optional so the existing
    hot-reload tests can keep injecting a stand-in publish loop without
    needing to materialise a log on disk.
    """
    async for event_payload in agent.run():
        if not isinstance(event_payload, Prediction):
            continue
        await producer.publish(
            STREAM_SIG_PREDICT, Event(type="prediction", payload=event_payload)
        )
        if prediction_log is not None and model_name is not None:
            prediction_log.append(
                agent_id=event_payload.agent_id,
                model_name=model_name,
                ts_event=event_payload.ts_event,
                horizon_ns=event_payload.horizon_ns,
                symbol=event_payload.symbol,
                direction=event_payload.direction,
                confidence=event_payload.confidence,
            )
        log.info(
            "gbm.pred",
            symbol=event_payload.symbol,
            direction=event_payload.direction,
            confidence=event_payload.confidence,
        )


async def _main() -> None:
    configure_logging()
    configure_tracing("agents.gbm_predictor")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await run(stop)
    finally:
        log.info("gbm.stop")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
