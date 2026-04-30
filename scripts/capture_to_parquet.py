"""
scripts/capture_to_parquet.py - record bars + features for retraining.

Tails ``md.bars.1m`` and ``features.online`` from Redis, joins each
bar with the matching FeatureFrame on ``(symbol, ts_event)``, and
writes batches of joined rows to ``data/captures/<run>_<batch>.parquet``.

The output schema is bit-identical to what
``agents.gbm_predictor.train`` expects::

  close, ret_1m, ret_5m, ret_15m, ret_60m,
  rv_5m, rv_30m, mom_z_30m, mom_z_240m,
  book_imbalance_1, spread_bps

Run alongside the live stack::

  uv run python scripts/capture_to_parquet.py

Then leave it running.  After 1-3 days of accumulation, retrain::

  uv run python -m agents.gbm_predictor.train \\
      --input "data/captures/*.parquet"

Design notes:
  - We start each consumer at ``$`` so we only capture data that
    arrives *after* the script starts.  Rerunning won't backfill or
    duplicate.
  - Buffer is keyed by ``(symbol, ts_event)``.  Whichever stream
    arrives first holds; the second arrival flushes a joined row.
  - Buffer entries older than ``MAX_PENDING_SEC`` (default 300s) are
    evicted as orphans, with a warning - this happens if e.g. the
    features service is down so bars never get matched.
  - Batches flush every ``flush_every`` rows OR every ``flush_secs``
    seconds, whichever comes first.  On Ctrl+C the partial buffer is
    flushed before exit.

This script is intentionally NOT a service - no heartbeat, no /services
entry.  It's an out-of-band capture tool.  Run it in its own pwsh
window when you want training data.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import pathlib
import signal
import sys
import time
import uuid
from collections import OrderedDict
from typing import Any

import polars as pl
from redis.asyncio import Redis

from agents.gbm_predictor.features import FEATURES
from fincept_bus.streams import STREAM_FEATURES_ONLINE, STREAM_MD_BARS_1M
from fincept_core.config import get_settings

MAX_PENDING_SEC = 300  # drop half-rows older than this


class JoinedBuffer:
    """In-memory join buffer keyed by (symbol, ts_event).

    Each entry collects a bar half (``close``) and a features half
    (``values``).  Once both arrive we emit a single row and remove
    the entry.  An OrderedDict preserves insertion order so we can
    cheaply evict oldest pending rows that never matched.
    """

    def __init__(self) -> None:
        self._pending: OrderedDict[tuple[str, int], dict[str, Any]] = OrderedDict()
        self.matched = 0
        self.bar_only_evicted = 0
        self.feat_only_evicted = 0

    def add_bar(self, symbol: str, ts_event: int, close: float) -> dict[str, Any] | None:
        key = (symbol, ts_event)
        entry = self._pending.get(key)
        if entry is None:
            self._pending[key] = {"close": close, "ts": time.time()}
            return None
        if "values" in entry:
            row = self._build_row(symbol, ts_event, close, entry["values"])
            self._pending.pop(key, None)
            self.matched += 1
            return row
        entry["close"] = close
        entry["ts"] = time.time()
        return None

    def add_features(
        self, symbol: str, ts_event: int, values: dict[str, float | None]
    ) -> dict[str, Any] | None:
        key = (symbol, ts_event)
        entry = self._pending.get(key)
        if entry is None:
            self._pending[key] = {"values": values, "ts": time.time()}
            return None
        if "close" in entry:
            row = self._build_row(symbol, ts_event, entry["close"], values)
            self._pending.pop(key, None)
            self.matched += 1
            return row
        entry["values"] = values
        entry["ts"] = time.time()
        return None

    def evict_stale(self, *, max_age_sec: float = MAX_PENDING_SEC) -> None:
        cutoff = time.time() - max_age_sec
        # Iterate from oldest; OrderedDict insertion order is reliable.
        while self._pending:
            (_key, entry) = next(iter(self._pending.items()))
            if entry["ts"] >= cutoff:
                break
            self._pending.popitem(last=False)
            if "close" in entry and "values" not in entry:
                self.bar_only_evicted += 1
            elif "values" in entry and "close" not in entry:
                self.feat_only_evicted += 1

    @staticmethod
    def _build_row(
        symbol: str, ts_event: int, close: float, values: dict[str, float | None]
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "symbol": symbol,
            "ts_event": ts_event,
            "close": close,
        }
        for name in FEATURES:
            v = values.get(name)
            row[name] = float(v) if v is not None else None
        return row

    @property
    def pending_count(self) -> int:
        return len(self._pending)


async def _consume_bars(
    redis: Redis[Any],
    buffer: JoinedBuffer,
    out_rows: list[dict[str, Any]],
    stop: asyncio.Event,
) -> None:
    """Tail md.bars.1m starting from `$` (live only)."""
    last_id = "$"
    while not stop.is_set():
        resp = await redis.xread(
            streams={STREAM_MD_BARS_1M: last_id},
            count=200,
            block=2_000,
        )
        if not resp:
            continue
        for _stream_name, entries in resp:
            for msg_id, fields in entries:
                last_id = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
                payload = _decode_payload(fields)
                bar = payload.get("payload") or payload
                symbol = bar.get("symbol")
                ts_event = bar.get("ts_event")
                close = bar.get("close")
                if symbol is None or ts_event is None or close is None:
                    continue
                row = buffer.add_bar(str(symbol), int(ts_event), float(close))
                if row is not None:
                    out_rows.append(row)


async def _consume_features(
    redis: Redis[Any],
    buffer: JoinedBuffer,
    out_rows: list[dict[str, Any]],
    stop: asyncio.Event,
) -> None:
    """Tail features.online starting from `$`."""
    last_id = "$"
    while not stop.is_set():
        resp = await redis.xread(
            streams={STREAM_FEATURES_ONLINE: last_id},
            count=200,
            block=2_000,
        )
        if not resp:
            continue
        for _stream_name, entries in resp:
            for msg_id, fields in entries:
                last_id = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
                payload = _decode_payload(fields)
                frame = payload.get("payload") or payload
                if frame.get("freq") != "1m":
                    continue
                symbol = frame.get("symbol")
                ts_event = frame.get("ts_event")
                values = frame.get("values") or {}
                if symbol is None or ts_event is None:
                    continue
                row = buffer.add_features(str(symbol), int(ts_event), values)
                if row is not None:
                    out_rows.append(row)


def _decode_payload(fields: dict[Any, Any]) -> dict[str, Any]:
    """Pull the JSON ``data`` field out of a Redis Stream entry.

    Producers wrap the event under a ``data`` field as JSON.  We
    accept both bytes and str for forward-compat with redis-py options.
    """
    import json as _json

    raw = fields.get(b"data") if isinstance(next(iter(fields), b""), bytes) else fields.get("data")
    if raw is None:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        decoded = _json.loads(raw)
    except _json.JSONDecodeError:
        return {}
    if isinstance(decoded, dict):
        return decoded
    return {}


async def _flusher(
    out_rows: list[dict[str, Any]],
    *,
    out_dir: pathlib.Path,
    run_id: str,
    flush_every: int,
    flush_secs: float,
    stop: asyncio.Event,
    buffer: JoinedBuffer,
) -> None:
    """Periodically dump accumulated rows to a fresh parquet file."""
    batch_idx = 0
    last_flush = time.time()
    while not stop.is_set():
        await asyncio.sleep(1.0)
        buffer.evict_stale()
        elapsed = time.time() - last_flush
        if len(out_rows) >= flush_every or (out_rows and elapsed >= flush_secs):
            batch_idx += _write_batch(out_rows, out_dir=out_dir, run_id=run_id, batch_idx=batch_idx)
            last_flush = time.time()
        if elapsed >= 30.0:
            print(
                f"[capture] matched={buffer.matched} pending={buffer.pending_count} "
                f"orphan_bars={buffer.bar_only_evicted} orphan_feats={buffer.feat_only_evicted}",
                flush=True,
            )
            last_flush = time.time()
    # Final flush.
    if out_rows:
        _write_batch(out_rows, out_dir=out_dir, run_id=run_id, batch_idx=batch_idx)


def _write_batch(
    rows: list[dict[str, Any]],
    *,
    out_dir: pathlib.Path,
    run_id: str,
    batch_idx: int,
) -> int:
    if not rows:
        return 0
    df = pl.DataFrame(rows)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_id}_b{batch_idx:06d}.parquet"
    df.write_parquet(path)
    print(f"[capture] wrote {path} rows={df.height}", flush=True)
    rows.clear()
    return 1


async def run(
    *,
    out_dir: pathlib.Path,
    run_id: str,
    flush_every: int,
    flush_secs: float,
) -> None:
    settings = get_settings()
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    buffer = JoinedBuffer()
    out_rows: list[dict[str, Any]] = []
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    bars_task = asyncio.create_task(_consume_bars(redis, buffer, out_rows, stop))
    feats_task = asyncio.create_task(_consume_features(redis, buffer, out_rows, stop))
    flush_task = asyncio.create_task(
        _flusher(
            out_rows,
            out_dir=out_dir,
            run_id=run_id,
            flush_every=flush_every,
            flush_secs=flush_secs,
            stop=stop,
            buffer=buffer,
        )
    )

    print(f"[capture] writing to {out_dir} run_id={run_id}", flush=True)
    print(
        f"[capture] tailing {STREAM_MD_BARS_1M} + {STREAM_FEATURES_ONLINE}; "
        f"Ctrl-C to stop and flush.",
        flush=True,
    )

    try:
        await stop.wait()
    finally:
        for t in (bars_task, feats_task, flush_task):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await redis.aclose()  # type: ignore[attr-defined]
        print(
            f"[capture] done.  matched={buffer.matched} pending={buffer.pending_count} "
            f"orphan_bars={buffer.bar_only_evicted} orphan_feats={buffer.feat_only_evicted}",
            flush=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser(prog="capture_to_parquet")
    parser.add_argument(
        "--out-dir",
        default="data/captures",
        help="Directory for batch parquet files (default data/captures).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Run identifier (default: timestamp-uuid).",
    )
    parser.add_argument(
        "--flush-every",
        type=int,
        default=200,
        help="Flush after this many joined rows accumulate.",
    )
    parser.add_argument(
        "--flush-secs",
        type=float,
        default=60.0,
        help="Flush at least every N seconds if any rows are pending.",
    )
    args = parser.parse_args()

    run_id = args.run_id or f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
    asyncio.run(
        run(
            out_dir=pathlib.Path(args.out_dir),
            run_id=run_id,
            flush_every=args.flush_every,
            flush_secs=args.flush_secs,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
