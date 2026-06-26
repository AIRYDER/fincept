"""
agents.regime_agent.main - long-running entrypoint.

  python -m agents.regime_agent.main

Per cycle (default 1 hour):

  1. Fetch latest VIXCLS, T10Y2Y, DFF observations from FRED.
  2. Run :func:`agents.regime_agent.rules.classify`.
  3. If the classified regime CHANGED since last cycle, publish a
     ``RegimeSignal`` to ``STREAM_SIG_REGIME``.  Skipping unchanged
     emissions cuts noise downstream.
  4. Heartbeat regardless of whether we emitted.

The agent intentionally does NOT publish on every cycle - the
orchestrator's ConsensusBuilder uses a max-age window, so a regime
that hasn't changed will keep contributing to consensus from its
last emission.  Re-emitting every hour with no state change just
churns Redis Streams memory.

Operationally OPT-IN: ``FRED_API_KEY`` must be set or the agent exits
cleanly at startup.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
from typing import Any

import httpx
from redis.asyncio import Redis

from agents.regime_agent.fred import latest_value
from agents.regime_agent.rules import (
    REGIME_DIRECTION,
    SERIES_DFF,
    SERIES_T10Y2Y,
    SERIES_VIX,
    RegimeView,
    classify,
)
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_SIG_REGIME
from fincept_core.clock import now_ns
from fincept_core.config import assert_safe_for_runtime, get_settings
from fincept_core.events import Event
from fincept_core.heartbeat import beat_periodically
from fincept_core.logging import configure_logging, get_logger
from fincept_core.schemas import RegimeSignal
from fincept_core.tracing import configure_tracing

log = get_logger(__name__)

AGENT_ID = "regime_agent.v1"

# Redis snapshot key that holds the full classifier view (regime label
# + raw FRED inputs + rationale).  The /regime API route reads this so
# the dashboard can show "VIX = 18.2, spread = 0.45 -> risk_on" rather
# than just "risk_on".  TTL is 4x the polling interval so a missed
# cycle doesn't immediately blank the panel.
REGIME_SNAPSHOT_KEY = "service:regime:latest"
SNAPSHOT_TTL_MULTIPLE = 4


async def _publish_snapshot(
    redis: Redis[Any], view: RegimeView, *, ts_ns: int, ttl_sec: int
) -> None:
    """Cache the latest classifier view as JSON on a TTL'd Redis key."""
    payload = {
        "agent_id": AGENT_ID,
        "ts_event": ts_ns,
        "regime": view.regime,
        "confidence": view.confidence,
        "vix": view.vix,
        "yield_spread": view.yield_spread,
        "fed_funds": view.fed_funds,
        "rationale": view.rationale,
        "direction_bias": REGIME_DIRECTION.get(view.regime, 0.0),
    }
    await redis.set(REGIME_SNAPSHOT_KEY, json.dumps(payload), ex=ttl_sec)


async def _fetch_view(http: httpx.AsyncClient, *, api_key: str) -> RegimeView:
    """Pull all three series in parallel, classify."""
    vix_task = latest_value(http, series_id=SERIES_VIX, api_key=api_key)
    spread_task = latest_value(http, series_id=SERIES_T10Y2Y, api_key=api_key)
    dff_task = latest_value(http, series_id=SERIES_DFF, api_key=api_key)
    vix, spread, dff = await asyncio.gather(vix_task, spread_task, dff_task)
    return classify(vix=vix, yield_spread=spread, fed_funds=dff)


async def run_loop(
    *,
    interval_sec: int,
    emit_unchanged: bool,
    stop: asyncio.Event,
) -> None:
    settings = get_settings()
    assert_safe_for_runtime(settings)
    if not settings.FRED_API_KEY:
        log.warning("regime.skip", reason="FRED_API_KEY unset")
        return

    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    producer = Producer(redis)
    heartbeat_task = asyncio.create_task(beat_periodically(redis, "regime_agent"))
    last_regime: str | None = None

    try:
        async with httpx.AsyncClient() as http:
            log.info(
                "regime.start",
                interval_sec=interval_sec,
                emit_unchanged=emit_unchanged,
            )
            while not stop.is_set():
                try:
                    view = await _fetch_view(http, api_key=settings.FRED_API_KEY)
                except httpx.HTTPError as exc:
                    log.warning("regime.fred_error", error=str(exc))
                    view = None

                if view is not None:
                    ts_now = now_ns()
                    # Always refresh the snapshot so the dashboard shows
                    # fresh VIX / spread numbers even when the classified
                    # label is stable.
                    await _publish_snapshot(
                        redis,
                        view,
                        ts_ns=ts_now,
                        ttl_sec=SNAPSHOT_TTL_MULTIPLE * interval_sec,
                    )
                    changed = view.regime != last_regime
                    if changed or emit_unchanged:
                        signal_obj = RegimeSignal(
                            agent_id=AGENT_ID,
                            ts_event=ts_now,
                            regime=view.regime,
                            confidence=view.confidence,
                        )
                        await producer.publish(
                            STREAM_SIG_REGIME,
                            Event(type="regime", payload=signal_obj),
                        )
                        log.info(
                            "regime.emitted",
                            regime=view.regime,
                            confidence=view.confidence,
                            vix=view.vix,
                            yield_spread=view.yield_spread,
                            rationale=view.rationale,
                            changed=changed,
                        )
                    else:
                        log.info(
                            "regime.unchanged",
                            regime=view.regime,
                            confidence=view.confidence,
                        )
                    last_regime = view.regime

                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=interval_sec)
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        await redis.aclose()  # type: ignore[attr-defined]


async def _main(args: argparse.Namespace) -> None:
    configure_logging()
    configure_tracing("regime_agent")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await run_loop(
            interval_sec=args.interval_sec,
            emit_unchanged=args.emit_unchanged,
            stop=stop,
        )
    finally:
        log.info("regime.stop")


def main() -> None:
    parser = argparse.ArgumentParser(prog="regime_agent.main")
    parser.add_argument(
        "--interval-sec",
        type=int,
        default=3600,
        help="Cycle period.  FRED data is daily; default 1h is plenty.",
    )
    parser.add_argument(
        "--emit-unchanged",
        action="store_true",
        help="Emit a RegimeSignal every cycle even when the regime label hasn't "
        "changed.  Off by default to avoid stream churn.",
    )
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
