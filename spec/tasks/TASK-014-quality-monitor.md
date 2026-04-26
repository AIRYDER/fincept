# TASK-014 · Quality monitor — gaps, cross-venue spread, staleness alerts

**Phase:** D · **Depends on:** TASK-010, TASK-012, TASK-013 · **Blocks:** Phase D checkpoint

## Goal

A long-running quality monitor that consumes `md.trades` and `md.books` from all configured venues and emits **AlertEvent**s when:

1. **Sequence gap** — venue `seq` jumps non-monotonically (data loss).
2. **Staleness** — no message for symbol on a venue for > N seconds during expected market hours.
3. **Cross-venue spread anomaly** — best bid/ask on Binance vs Coinbase vs Kraken diverges > X% (suggests one venue is broken or has stale data).
4. **Clock skew** — `ts_recv − ts_event` exceeds budget (e.g., > 1 s p99 on crypto).

Monitor is **read-only** with respect to the streams; it produces `AlertEvent` to a separate `events.alerts` stream and Prometheus metrics.

## Files to create

```
services/ingestor/src/ingestor/quality.py        # already stub from TASK-010 — flesh out here
services/ingestor/src/ingestor/quality_main.py   # standalone entrypoint
services/ingestor/tests/test_quality.py
libs/fincept-core/src/fincept_core/schemas.py    # add AlertEvent (extend; do NOT mutate existing schemas)
```

## Contract additions to `fincept-core` schemas

`AlertEvent` is a new event type; add to `spec/CONTRACTS.md §3` and to `schemas.py`:

```python
class AlertEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: int = 1
    alert_id: str                         # ULID
    ts_event: int
    severity: str                         # "info" | "warning" | "critical"
    source: str                           # "ingestor.quality", "risk.gate", etc.
    code: str                             # machine-readable: "seq_gap", "stale", "cross_spread", "clock_skew"
    message: str                          # human-readable
    tags: dict[str, str] = Field(default_factory=dict)  # e.g., {"venue":"binance","symbol":"BTC-USD"}
```

Add `STREAM_ALERTS = "events.alerts"` to `fincept-bus/streams.py`.

## Contracts

### `quality.py`

```python
import asyncio
from collections import defaultdict
from decimal import Decimal
from fincept_core.clock import now_ns
from fincept_core.config import get_settings
from fincept_core.ids import new_id
from fincept_core.logging import get_logger
from fincept_core.schemas import TradeEvent, BookSnapshotEvent, BookDeltaEvent, AlertEvent
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_ALERTS
from redis.asyncio import Redis

log = get_logger(__name__)

# Defaults (override in config)
STALENESS_BUDGET_NS = 30_000_000_000              # 30 s
CLOCK_SKEW_BUDGET_NS = 1_000_000_000              # 1 s
CROSS_SPREAD_THRESHOLD_BPS = 50                   # 50 bps = 0.5%
SEQ_GAP_TOLERATE = 0                              # any non-monotonic = alert

class QualityMonitor:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self.producer = Producer(redis)
        self._last_ts: dict[tuple[str, str], int] = {}            # (venue, symbol) → ns
        self._last_seq: dict[tuple[str, str], int] = {}           # (venue, symbol) → seq
        self._last_top: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
        # (venue, symbol) → (best_bid, best_ask)

    async def on_trade(self, ev: TradeEvent) -> None:
        key = (ev.venue.value, ev.symbol)
        # Sequence gap
        prev_seq = self._last_seq.get(key)
        if ev.seq is not None and prev_seq is not None and ev.seq != prev_seq + 1:
            await self._emit("seq_gap", "warning",
                             f"seq gap on {key}: {prev_seq} → {ev.seq}",
                             {"venue": ev.venue.value, "symbol": ev.symbol,
                              "prev": str(prev_seq), "current": str(ev.seq)})
        if ev.seq is not None:
            self._last_seq[key] = ev.seq

        # Staleness baseline
        self._last_ts[key] = ev.ts_recv

        # Clock skew
        skew = ev.ts_recv - ev.ts_event
        if skew > CLOCK_SKEW_BUDGET_NS:
            await self._emit("clock_skew", "warning",
                             f"clock skew on {key}: {skew/1e9:.2f}s",
                             {"venue": ev.venue.value, "symbol": ev.symbol,
                              "skew_ns": str(skew)})

    async def on_book(self, ev: BookSnapshotEvent | BookDeltaEvent) -> None:
        key = (ev.venue.value, ev.symbol)
        self._last_ts[key] = ev.ts_recv
        # Track top-of-book ONLY on snapshots (deltas would require maintaining the book here)
        if isinstance(ev, BookSnapshotEvent) and ev.bids and ev.asks:
            best_bid = max(b.price for b in ev.bids)
            best_ask = min(a.price for a in ev.asks)
            self._last_top[key] = (best_bid, best_ask)
            await self._check_cross_venue(ev.symbol)

    async def _check_cross_venue(self, symbol: str) -> None:
        """Compare best bid/ask across all venues for this symbol."""
        tops = {v: self._last_top[(v, symbol)] for (v, s) in self._last_top.keys()
                if s == symbol and (v, s) in self._last_top}
        if len(tops) < 2:
            return
        mids = {v: (b + a) / 2 for v, (b, a) in tops.items()}
        max_mid = max(mids.values())
        min_mid = min(mids.values())
        spread_bps = (max_mid - min_mid) / min_mid * Decimal(10000)
        if spread_bps > Decimal(CROSS_SPREAD_THRESHOLD_BPS):
            await self._emit("cross_spread", "critical",
                             f"cross-venue mid divergence {spread_bps:.1f} bps on {symbol}",
                             {"symbol": symbol, "spread_bps": str(spread_bps),
                              **{f"mid_{v}": str(m) for v, m in mids.items()}})

    async def staleness_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            now = now_ns()
            for key, ts in list(self._last_ts.items()):
                if now - ts > STALENESS_BUDGET_NS:
                    venue, symbol = key
                    await self._emit("stale", "warning",
                                     f"no events on {venue}:{symbol} for {(now-ts)/1e9:.1f}s",
                                     {"venue": venue, "symbol": symbol,
                                      "silent_ns": str(now - ts)})

    async def _emit(self, code: str, severity: str, message: str, tags: dict[str, str]) -> None:
        ev = AlertEvent(
            alert_id=new_id(), ts_event=now_ns(), severity=severity,
            source="ingestor.quality", code=code, message=message, tags=tags,
        )
        await self.producer.publish(STREAM_ALERTS, ev)
        log.warning("quality.alert", code=code, severity=severity, message=message, **tags)
```

### `quality_main.py`

Standalone process: subscribes to `md.trades` + `md.books` consumer group, dispatches to `QualityMonitor`, runs `staleness_loop` concurrently. Auto-restart on consumer error.

## Tests

### `tests/test_quality.py`

```python
import pytest
from decimal import Decimal
from redis.asyncio import Redis
from fincept_core.schemas import TradeEvent, Venue, AssetClass, BookSnapshotEvent, BookLevel
from fincept_bus.streams import STREAM_ALERTS
from ingestor.quality import QualityMonitor

@pytest.mark.asyncio
async def test_seq_gap_detected():
    r = Redis.from_url("redis://localhost:6379/15")
    await r.delete(STREAM_ALERTS)
    q = QualityMonitor(r)
    await q.on_trade(TradeEvent(
        venue=Venue.BINANCE, symbol="BTC-USD", asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=1, ts_recv=2, seq=1, price=Decimal("100"), size=Decimal("0.1"),
    ))
    await q.on_trade(TradeEvent(
        venue=Venue.BINANCE, symbol="BTC-USD", asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=3, ts_recv=4, seq=10, price=Decimal("100"), size=Decimal("0.1"),  # gap: expected 2
    ))
    # The alert was published; read it back
    msgs = await r.xrange(STREAM_ALERTS, count=10)
    assert any(b"seq_gap" in v.get(b"payload_json", b"") for _, v in msgs)
    await r.aclose()

@pytest.mark.asyncio
async def test_cross_spread_alert():
    r = Redis.from_url("redis://localhost:6379/15")
    await r.delete(STREAM_ALERTS)
    q = QualityMonitor(r)
    base = dict(symbol="BTC-USD", asset_class=AssetClass.CRYPTO_SPOT, ts_event=1, ts_recv=2)
    await q.on_book(BookSnapshotEvent(venue=Venue.BINANCE, **base,
                                      bids=[BookLevel(price=Decimal("100000"), size=Decimal("1"))],
                                      asks=[BookLevel(price=Decimal("100100"), size=Decimal("1"))]))
    await q.on_book(BookSnapshotEvent(venue=Venue.COINBASE, **base,
                                      bids=[BookLevel(price=Decimal("99500"), size=Decimal("1"))],
                                      asks=[BookLevel(price=Decimal("99600"), size=Decimal("1"))]))
    msgs = await r.xrange(STREAM_ALERTS, count=10)
    assert any(b"cross_spread" in v.get(b"payload_json", b"") for _, v in msgs)
    await r.aclose()
```

## Landmines

- **Cross-spread on illiquid pairs is naturally noisy.** Configure per-symbol thresholds in `Settings` (default 50 bps for majors, 200 bps for thin pairs). Hard-coded one-size-fits-all triggers alert fatigue.
- **Staleness during legitimate halts** (NYSE close, exchange maintenance windows): operator must be able to silence per (venue, symbol, time-window). Add a `quality_silences` table later (Phase H).
- **Sequence gap on Coinbase L2:** Coinbase L2 does not provide a numeric sequence per message; staleness + cross-spread carry the load there. Document in code comment.
- **Alert flood control:** dedup identical (code, tags) within a 30 s window; emit a "still-active" event every 60 s instead of repeating.
- **Cross-venue requires SAME symbol semantics:** ensure `BTC-USD` everywhere — Binance `BTC-USDT` vs Coinbase `BTC-USD` are NOT the same and must not be compared. Maintain a separate "tether-stable" group if applicable.

## Out of scope

- Self-healing (auto-reconnect of broken venue): part of TASK-010's adapter loop, not the monitor.
- Alert-routing to PagerDuty/Slack — Phase H (TASK-073).
- Persistent alert log — Phase H (TASK-074); v1 lives only on the `events.alerts` stream.

## Done when

- [ ] `quality.py` and `quality_main.py` exist
- [ ] `AlertEvent` added to `fincept-core/schemas.py` and to `spec/CONTRACTS.md §3`
- [ ] `STREAM_ALERTS` added to `fincept-bus/streams.py`
- [ ] `pytest services/ingestor/tests/test_quality.py` is green
- [ ] `mypy services/ingestor` is green
- [ ] Phase D checkpoint: 24-hr soak test on 5 crypto pairs across 3 venues with NO `seq_gap` or `stale` alerts during normal operation
