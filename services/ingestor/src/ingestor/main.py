"""
ingestor.main — entrypoint with reconnect + signal handling.

The hot loop is intentionally small:

    while not stop:
        await adapter.connect()
        async for event in adapter.stream():
            quality.observe(...)
            await writer.handle(event)
        # If we fall through, the stream ended (WS close).  Reconnect.

A simple capped-exponential backoff handles flaky networks.  Snapshot
sync (depth-update gap recovery) is out of scope here — it's the
responsibility of TASK-014 (quality monitor).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from typing import Any

from redis.asyncio import Redis

from fincept_core.config import assert_safe_for_runtime, get_settings
from fincept_core.heartbeat import beat_periodically
from fincept_core.logging import configure_logging, get_logger
from fincept_core.tracing import configure_tracing
from ingestor.base import VenueAdapter
from ingestor.binance import BinanceAdapter
from ingestor.coinbase import CoinbaseAdapter
from ingestor.kraken import KrakenAdapter
from ingestor.quality import LatencyTracker
from ingestor.writer import Writer

VENUE_ADAPTERS: dict[str, type[VenueAdapter]] = {
    "binance": BinanceAdapter,
    "coinbase": CoinbaseAdapter,
    "kraken": KrakenAdapter,
}

log = get_logger(__name__)

INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0


async def run_loop(
    adapter: VenueAdapter,
    writer: Writer,
    latency: LatencyTracker,
    stop: asyncio.Event,
    *,
    initial_backoff_s: float = INITIAL_BACKOFF_S,
    max_backoff_s: float = MAX_BACKOFF_S,
) -> None:
    """Connect/stream/handle loop with capped exponential backoff on errors."""
    backoff = initial_backoff_s
    while not stop.is_set():
        try:
            await adapter.connect()
        except Exception as exc:
            err_str = str(exc)
            log.warning("ingestor.connect_failed", error=err_str, retry_in_s=backoff)
            # HTTP 451 = "Unavailable For Legal Reasons" -- typical for
            # Binance from US IPs.  Surface a one-line actionable hint
            # so the operator doesn't sit watching exponential backoff.
            if "451" in err_str:
                log.warning(
                    "ingestor.geo_blocked",
                    hint=(
                        "venue rejected with HTTP 451 (geo-block). "
                        "Try --venue coinbase or --venue kraken; both "
                        "work from US IPs without an account."
                    ),
                )
            await _sleep_or_stop(backoff, stop)
            backoff = min(backoff * 2, max_backoff_s)
            continue

        backoff = initial_backoff_s  # reset on a successful connect
        try:
            async for event in adapter.stream():
                if stop.is_set():
                    break
                ts_event = getattr(event, "ts_event", None)
                ts_recv = getattr(event, "ts_recv", None)
                seq = getattr(event, "seq", None)
                venue = getattr(event, "venue", None)
                symbol = getattr(event, "symbol", None)
                if ts_event is not None and ts_recv is not None and venue is not None:
                    latency.observe(
                        venue=str(getattr(venue, "value", venue)),
                        symbol=str(symbol) if symbol is not None else "",
                        seq=int(seq) if seq is not None else None,
                        ts_event=int(ts_event),
                        ts_recv=int(ts_recv),
                    )
                await writer.handle(event)
        except Exception as exc:
            log.warning("ingestor.stream_failed", error=str(exc))
        finally:
            await adapter.close()

        if stop.is_set():
            break
        # Stream ended (WS close) — back off briefly before reconnecting.
        await _sleep_or_stop(initial_backoff_s, stop)

    await writer.flush()


async def _sleep_or_stop(seconds: float, stop: asyncio.Event) -> None:
    """Sleep for *seconds* but wake immediately when ``stop`` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return


async def _main(venue: str) -> None:
    configure_logging()
    configure_tracing("ingestor")
    settings = get_settings()
    assert_safe_for_runtime(settings)
    if not settings.UNIVERSE:
        raise RuntimeError("FINCEPT_UNIVERSE is empty; nothing to ingest")

    adapter_cls = VENUE_ADAPTERS.get(venue)
    if adapter_cls is None:
        raise ValueError(f"Unknown venue {venue!r}; supported: {sorted(VENUE_ADAPTERS)}")

    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    adapter = adapter_cls(list(settings.UNIVERSE))
    writer = Writer(redis)
    latency = LatencyTracker()
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows doesn't support add_signal_handler in asyncio.  On that
        # platform devs use Ctrl-C in PowerShell which raises KeyboardInterrupt
        # at the asyncio.run boundary.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    def ingestor_stats() -> dict[str, Any]:
        """Collect ingestor metrics for heartbeat."""
        trades_pending, books_pending = writer.pending
        trades_dropped, books_dropped = writer.dropped
        snapshots = latency.snapshot()
        return {
            "buffer": {
                "trades_pending": trades_pending,
                "books_pending": books_pending,
                "trades_dropped": trades_dropped,
                "books_dropped": books_dropped,
            },
            "channels": len(snapshots),
            "total_gaps": sum(s.total_gaps for s in snapshots),
            "max_latency_ns": max((s.max_latency_ns for s in snapshots), default=0),
        }

    heartbeat_task = asyncio.create_task(
        beat_periodically(redis, "ingestor", stats_callback=ingestor_stats)
    )
    try:
        await run_loop(adapter, writer, latency, stop)
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        await redis.aclose()  # type: ignore[attr-defined]


def main() -> None:
    """Synchronous CLI entrypoint.

    ``python -m ingestor.main --venue {binance,coinbase,kraken}`` selects
    which adapter to run.  Only one venue per process for now — a future
    task will fan multiple venues into a single supervisor.
    """
    parser = argparse.ArgumentParser(prog="ingestor")
    parser.add_argument(
        "--venue",
        choices=sorted(VENUE_ADAPTERS),
        default="binance",
        help="venue adapter to run (default: binance)",
    )
    args = parser.parse_args()
    asyncio.run(_main(args.venue))


if __name__ == "__main__":
    main()
