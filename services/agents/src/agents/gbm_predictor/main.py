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
import dataclasses
import json
import logging
import os
import pathlib
import signal
from typing import Any, cast

from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_SIG_PREDICT
from fincept_core.config import assert_safe_for_runtime, get_settings
from fincept_core.datasets import FeatureRow, FeatureSnapshot, FeatureSnapshotStore
from fincept_core.events import Event
from fincept_core.heartbeat import beat_periodically
from fincept_core.logging import configure_logging, get_logger
from fincept_core.prediction_log import PredictionLog, PredictionRow, _validate_agent_id
from fincept_core.schemas import Prediction
from fincept_core.tracing import configure_tracing
from redis.asyncio import Redis

from agents.gbm_predictor.features import _compute_feature_schema_hash
from agents.gbm_predictor.infer import GBMPredictor

log = get_logger(__name__)

AGENT_ID = "gbm_predictor.v1"
DEFAULT_MODEL_DIR = "models/gbm_predictor"

# Poll interval for the active.json watcher.  30s is friendly to the
# filesystem (one stat per cycle), responsive enough for an operator
# clicking Promote (worst case ~30s to take effect), and easy to crank
# down from tests via the env var without monkey-patching.
DEFAULT_RELOAD_POLL_S = 30.0


# --------------------------------------------------------------------------- #
# Feature-availability sidecar (Phase: ml-dataset-evidence-spine, todo 9)     #
# --------------------------------------------------------------------------- #


def _default_feature_health_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("FEATURE_HEALTH_DIR", "data/feature_health"))


@dataclasses.dataclass(frozen=True)
class FeatureHealthRow:
    """One persisted feature-availability diagnostic.

    A sidecar to :class:`fincept_core.prediction_log.PredictionRow`:
    joined by ``prediction_id`` (the ``PredictionRow.id`` returned by
    ``prediction_log.append``).  Kept in a separate JSONL file so the
    prediction log schema stays stable and the health rows can be
    truncated / rotated independently.
    """

    prediction_id: str
    ts_event: int
    symbol: str
    missing: list[str]
    defaulted: list[str]
    aliased: list[str]

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> FeatureHealthRow:
        data = json.loads(line)
        return cls(
            prediction_id=str(data["prediction_id"]),
            ts_event=int(data["ts_event"]),
            symbol=str(data["symbol"]),
            missing=list(data.get("missing", [])),
            defaulted=list(data.get("defaulted", [])),
            aliased=list(data.get("aliased", [])),
        )


class FeatureHealthLog:
    """Append-only feature-availability record on the filesystem.

    Mirrors the shape of :class:`fincept_core.prediction_log.PredictionLog`
    but writes to ``data/feature_health/<agent_id>.jsonl``.  The write
    is best-effort from the publish loop's perspective: a failure is
    logged as ``feature_health_write_failed`` and never propagates --
    a broken health sidecar must not stop predictions from being
    published or recorded.
    """

    def __init__(self, *, health_dir: pathlib.Path | None = None) -> None:
        self._health_dir = health_dir or _default_feature_health_dir()

    @property
    def health_dir(self) -> pathlib.Path:
        return self._health_dir

    def _path(self, agent_id: str) -> pathlib.Path:
        _validate_agent_id(agent_id)
        return self._health_dir / f"{agent_id}.jsonl"

    def append(
        self,
        *,
        agent_id: str,
        prediction_id: str,
        ts_event: int,
        symbol: str,
        missing: list[str],
        defaulted: list[str],
        aliased: list[str],
    ) -> FeatureHealthRow:
        _validate_agent_id(agent_id)
        if not isinstance(prediction_id, str) or not prediction_id:
            raise ValueError("prediction_id must be a non-empty string")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("symbol must be a non-empty string")

        self._health_dir.mkdir(parents=True, exist_ok=True)
        row = FeatureHealthRow(
            prediction_id=prediction_id,
            ts_event=ts_event,
            symbol=symbol,
            missing=list(missing),
            defaulted=list(defaulted),
            aliased=list(aliased),
        )
        path = self._path(agent_id)
        with path.open("a", encoding="utf-8") as f:
            f.write(row.to_json() + "\n")
        return row

    def read(self, *, agent_id: str, limit: int = 200) -> list[FeatureHealthRow]:
        """Return the most-recent ``limit`` health rows for an agent.

        Tolerant of malformed lines (skipped, matching the prediction
        log's resilience pattern) so a partially-corrupted file never
        takes the read down.
        """
        if limit < 1:
            raise ValueError("limit must be >= 1")
        path = self._path(agent_id)
        if not path.is_file():
            return []
        rows: list[FeatureHealthRow] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(FeatureHealthRow.from_json(line))
                except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                    continue
        return rows[-limit:]


def _models_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("MODELS_DIR", "models"))


def _active_dir() -> pathlib.Path:
    """Where the active + shadow pointers live.  Override is for tests."""
    override = os.environ.get("ACTIVE_MODELS_DIR")
    if override:
        return pathlib.Path(override)
    return _models_root() / "active"


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
    models_root = _models_root()
    pointer = _active_dir() / f"{AGENT_ID}.json"
    if pointer.is_file():
        try:
            data = json.loads(pointer.read_text())
            name = data.get("model_name") if isinstance(data, dict) else None
            if isinstance(name, str) and name:
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


def _resolve_shadow_model_dir() -> pathlib.Path | None:
    """Pick the SHADOW model directory if one is set, else ``None``.

    Phase E2: a shadow binding (set via ``POST /models/{name}/shadow``)
    causes the agent to run a second inference loop in parallel.  Its
    predictions are recorded to the JSONL store but NOT published to
    Redis -- the orchestrator must never see shadow predictions.

    Returns ``None`` (the most common state) when:
      * the shadow pointer file doesn't exist, OR
      * the file is malformed (logged as a warning).

    Unlike ``_resolve_model_dir`` there is no env-var fallback: shadow
    is an explicit operator action, not a default state.
    """
    pointer = _active_dir() / f"{AGENT_ID}.shadow.json"
    if not pointer.is_file():
        return None
    try:
        data = json.loads(pointer.read_text())
        name = data.get("model_name") if isinstance(data, dict) else None
        if isinstance(name, str) and name:
            resolved = _models_root() / name
            log.info(
                "gbm.shadow.from_pointer",
                pointer=str(pointer),
                model=name,
            )
            return resolved
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        logging.getLogger(__name__).warning(
            "gbm.shadow.pointer_ignored: %s (%s)", pointer, exc
        )
    return None


async def _build_agent(model_dir: pathlib.Path, redis: Redis[Any]) -> GBMPredictor:
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
    shadow_loop: Any = None,
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
      2. Spawn the active publish task (yields predictions until
         cancelled).  If a shadow pointer is also present, build a
         second agent and spawn a parallel shadow task that records
         predictions to the same JSONL log but does NOT publish to
         Redis.
      3. Loop: sleep for the reload poll interval, re-resolve both
         pointers.  Active and shadow are managed independently --
         changing one never disturbs the other.
      4. On stop: cancel all tasks, teardown all loaded agents, close
         Redis.

    This shape keeps the failure mode strict on cold-start (a missing
    active model is a hard exit) while making in-flight reloads
    forgiving (a bad pointer becomes a logged warning).  A failed
    SHADOW load is always a warning, never fatal -- shadow is a
    candidate, not the production path.
    """
    settings = get_settings()
    assert_safe_for_runtime(settings)
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
    if shadow_loop is None:
        shadow_loop = _shadow_loop
    if heartbeat is None:
        heartbeat = beat_periodically

    producer = Producer(redis)
    # The prediction log is shared across reloads -- it's keyed by
    # (agent_id, model_name) inside, so each appended row carries the
    # name of the booster that emitted it even after a hot-reload.
    prediction_log = PredictionLog()
    # Feature-availability sidecar: one JSONL row per emitted prediction
    # recording which requested features were missing / defaulted /
    # aliased.  Best-effort -- a write failure is logged and never
    # blocks the publish loop (see _publish_loop).
    feature_health_log = FeatureHealthLog()
    # Feature-snapshot store: one FeatureSnapshot per emitted prediction
    # recording the exact feature rows the agent saw at decision time
    # (the evidence spine's "what the agent saw" leg).  Best-effort --
    # a write failure is logged and never blocks the publish loop.
    feature_snapshot_store = FeatureSnapshotStore()

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
            feature_health_log=feature_health_log,
            feature_snapshot_store=feature_snapshot_store,
            model_name=current_dir.name,
        )
    )
    heartbeat_task = asyncio.create_task(heartbeat(redis, "gbm_predictor"))

    # Shadow slot starts unbound; loaded below if a pointer exists.
    # All shadow state lives in three locals that move together --
    # they are always in one of two consistent states:
    #     (None, None, None)             -- no shadow loaded
    #     (Path, GBMPredictor, Task)     -- shadow loaded and running
    current_shadow_dir: pathlib.Path | None = None
    current_shadow_agent: GBMPredictor | None = None
    shadow_task: asyncio.Task[None] | None = None

    initial_shadow_dir = _resolve_shadow_model_dir()
    if initial_shadow_dir is not None:
        try:
            current_shadow_agent = await build_agent(initial_shadow_dir, redis)
            shadow_task = asyncio.create_task(
                shadow_loop(
                    current_shadow_agent,
                    prediction_log=prediction_log,
                    model_name=initial_shadow_dir.name,
                    feature_snapshot_store=feature_snapshot_store,
                )
            )
            current_shadow_dir = initial_shadow_dir
            log.info("gbm.shadow.loaded", model_dir=str(initial_shadow_dir))
        except (FileNotFoundError, OSError) as exc:
            log.warning(
                "gbm.shadow.load_failed",
                shadow_dir=str(initial_shadow_dir),
                error=str(exc),
            )

    try:
        while True:
            stopped = await _wait_or_timeout(stop, poll_interval_s)
            if stopped:
                break

            # ---- Active slot reload ----------------------------------- #

            new_dir = _resolve_model_dir()
            if new_dir != current_dir:
                # Build the *new* agent before tearing down the old one
                # -- if setup() raises, we keep serving predictions
                # from the currently-loaded model and just log it.
                try:
                    new_agent = await build_agent(new_dir, redis)
                except (FileNotFoundError, OSError) as exc:
                    log.warning(
                        "gbm.reload_failed",
                        new_dir=str(new_dir),
                        error=str(exc),
                    )
                else:
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
                            feature_health_log=feature_health_log,
                            feature_snapshot_store=feature_snapshot_store,
                            model_name=current_dir.name,
                        )
                    )

            # ---- Shadow slot reload ----------------------------------- #
            #
            # Four transitions to handle:
            #   None  -> None   (most common; no-op)
            #   None  -> Path   (operator just set a shadow)
            #   Path  -> None   (operator cleared shadow)
            #   Path  -> Path'  (operator switched shadow candidate)
            new_shadow_dir = _resolve_shadow_model_dir()
            if new_shadow_dir != current_shadow_dir:
                # Build new shadow first (if any) so a failure leaves
                # the previous shadow untouched.
                new_shadow_agent: GBMPredictor | None = None
                if new_shadow_dir is not None:
                    try:
                        new_shadow_agent = await build_agent(new_shadow_dir, redis)
                    except (FileNotFoundError, OSError) as exc:
                        log.warning(
                            "gbm.shadow.load_failed",
                            shadow_dir=str(new_shadow_dir),
                            error=str(exc),
                        )
                        # Don't change current_shadow_*.  Next poll
                        # may either resolve to None (operator
                        # cleared the bad pointer) or to a fixed dir.
                        continue

                # Tear down the previous shadow if there was one.
                if shadow_task is not None:
                    log.info(
                        "gbm.shadow.swap",
                        from_dir=(
                            str(current_shadow_dir) if current_shadow_dir else None
                        ),
                        to_dir=(str(new_shadow_dir) if new_shadow_dir else None),
                    )
                    shadow_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await shadow_task
                    if current_shadow_agent is not None:
                        await current_shadow_agent.teardown()
                    shadow_task = None
                    current_shadow_agent = None

                # Spawn the new shadow task if we have one.
                if new_shadow_agent is not None and new_shadow_dir is not None:
                    current_shadow_agent = new_shadow_agent
                    current_shadow_dir = new_shadow_dir
                    shadow_task = asyncio.create_task(
                        shadow_loop(
                            current_shadow_agent,
                            prediction_log=prediction_log,
                            model_name=new_shadow_dir.name,
                            feature_snapshot_store=feature_snapshot_store,
                        )
                    )
                    log.info(
                        "gbm.shadow.loaded",
                        model_dir=str(new_shadow_dir),
                    )
                else:
                    current_shadow_dir = None
    finally:
        # Cancel & await all running tasks, then teardown all agents.
        running_tasks: list[asyncio.Task[Any]] = [heartbeat_task, publish_task]
        if shadow_task is not None:
            running_tasks.append(shadow_task)
        for task in running_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await current_agent.teardown()
        if current_shadow_agent is not None:
            await current_shadow_agent.teardown()
        await cast(Any, redis).aclose()


async def _publish_loop(
    agent: GBMPredictor,
    producer: Producer,
    *,
    prediction_log: PredictionLog | None = None,
    feature_health_log: FeatureHealthLog | None = None,
    feature_snapshot_store: FeatureSnapshotStore | None = None,
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

    ``feature_health_log`` (todo 9) records a FeatureHealthRow sidecar
    per emitted prediction.  Its write is best-effort: any failure is
    logged as ``feature_health_write_failed`` and swallowed so a broken
    health sidecar never stops predictions from being published or
    recorded.  The health snapshot is read from
    ``agent.last_feature_health`` (set by ``GBMPredictor.run`` on every
    cycle right before the Prediction is yielded).

    ``feature_snapshot_store`` (ml-dataset-evidence-spine, todo 4)
    records a :class:`FeatureSnapshot` per emitted prediction -- the
    "what the agent saw" leg of the evidence receipt.  Its write is
    likewise best-effort: a failure is logged as
    ``feature_snapshot_write_failed`` and swallowed.  The snapshot is
    built from ``agent.last_feature_vector`` and
    ``agent.last_feature_frame_ts`` (set by ``GBMPredictor.run`` on
    every cycle) and is de-duplicated by prediction_id via
    :meth:`FeatureSnapshotStore.append_if_missing`.
    """
    async for event_payload in agent.run():
        if not isinstance(event_payload, Prediction):
            continue
        await producer.publish(
            STREAM_SIG_PREDICT, Event(type="prediction", payload=event_payload)
        )
        pred_row: PredictionRow | None = None
        if prediction_log is not None and model_name is not None:
            pred_row = prediction_log.append(
                agent_id=event_payload.agent_id,
                model_name=model_name,
                ts_event=event_payload.ts_event,
                horizon_ns=event_payload.horizon_ns,
                symbol=event_payload.symbol,
                direction=event_payload.direction,
                confidence=event_payload.confidence,
            )
        if feature_health_log is not None and pred_row is not None:
            health = getattr(agent, "last_feature_health", None)
            if health is not None:
                try:
                    feature_health_log.append(
                        agent_id=event_payload.agent_id,
                        prediction_id=pred_row.id,
                        ts_event=event_payload.ts_event,
                        symbol=event_payload.symbol,
                        missing=list(health.missing),
                        defaulted=list(health.defaulted),
                        aliased=list(health.aliased),
                    )
                except Exception as exc:
                    # Best-effort: a broken health sidecar must never
                    # stop predictions from being published/recorded.
                    log.warning(
                        "feature_health_write_failed",
                        agent_id=event_payload.agent_id,
                        symbol=event_payload.symbol,
                        error=str(exc),
                    )
        if feature_snapshot_store is not None and pred_row is not None:
            feature_vector = getattr(agent, "last_feature_vector", None)
            frame_ts = getattr(agent, "last_feature_frame_ts", None)
            feature_names = getattr(agent, "_features", None)
            if (
                feature_vector is not None
                and frame_ts is not None
                and feature_names is not None
            ):
                try:
                    feature_row = FeatureRow(
                        symbol=event_payload.symbol,
                        ts=frame_ts,
                        features=dict(feature_vector),
                    )
                    snapshot = FeatureSnapshot(
                        decision_time_ns=event_payload.ts_event,
                        rows=[feature_row],
                        feature_schema_hash=_compute_feature_schema_hash(
                            list(feature_names)
                        ),
                    )
                    feature_snapshot_store.append_if_missing(
                        pred_row.id,
                        snapshot,
                        agent_id=event_payload.agent_id,
                    )
                except Exception as exc:
                    # Best-effort: a broken snapshot store must never
                    # stop predictions from being published/recorded.
                    log.warning(
                        "feature_snapshot_write_failed",
                        agent_id=event_payload.agent_id,
                        symbol=event_payload.symbol,
                        error=str(exc),
                    )
        log.info(
            "gbm.pred",
            symbol=event_payload.symbol,
            direction=event_payload.direction,
            confidence=event_payload.confidence,
        )


async def _shadow_loop(
    agent: GBMPredictor,
    *,
    prediction_log: PredictionLog,
    model_name: str,
    feature_snapshot_store: FeatureSnapshotStore | None = None,
) -> None:
    """Run a shadow agent's inference loop -- record only, never publish.

    The crucial invariant of the shadow slot (Phase E2): predictions
    from the shadow booster must NEVER reach ``STREAM_SIG_PREDICT`` --
    the orchestrator would treat them as live signals and trade on
    them.  Defence in depth: this loop has no ``producer`` parameter
    at all, so there is no path -- not even an exception path -- by
    which a shadow prediction can land in Redis.

    The recorded JSONL row carries the shadow model's name, so the
    dashboard can compare active-vs-shadow per model without joining
    against the promotion history.
    """
    async for event_payload in agent.run():
        if not isinstance(event_payload, Prediction):
            continue
        pred_row = prediction_log.append(
            agent_id=event_payload.agent_id,
            model_name=model_name,
            ts_event=event_payload.ts_event,
            horizon_ns=event_payload.horizon_ns,
            symbol=event_payload.symbol,
            direction=event_payload.direction,
            confidence=event_payload.confidence,
        )
        if feature_snapshot_store is not None and pred_row is not None:
            feature_vector = getattr(agent, "last_feature_vector", None)
            frame_ts = getattr(agent, "last_feature_frame_ts", None)
            feature_names = getattr(agent, "_features", None)
            if (
                feature_vector is not None
                and frame_ts is not None
                and feature_names is not None
            ):
                try:
                    feature_row = FeatureRow(
                        symbol=event_payload.symbol,
                        ts=frame_ts,
                        features=dict(feature_vector),
                    )
                    snapshot = FeatureSnapshot(
                        decision_time_ns=event_payload.ts_event,
                        rows=[feature_row],
                        feature_schema_hash=_compute_feature_schema_hash(
                            list(feature_names)
                        ),
                    )
                    feature_snapshot_store.append_if_missing(
                        pred_row.id,
                        snapshot,
                        agent_id=event_payload.agent_id,
                    )
                except Exception as exc:
                    log.warning(
                        "feature_snapshot_write_failed",
                        agent_id=event_payload.agent_id,
                        symbol=event_payload.symbol,
                        error=str(exc),
                    )
        log.info(
            "gbm.shadow.pred",
            symbol=event_payload.symbol,
            direction=event_payload.direction,
            confidence=event_payload.confidence,
            shadow_model=model_name,
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
