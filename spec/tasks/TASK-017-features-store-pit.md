# TASK-017 · Feature store (online + offline) with point-in-time joins

**Phase:** D · **Depends on:** TASK-016 · **Blocks:** TASK-031 (GBM training + inference), Phase D checkpoint

## Goal

Persist `FeatureFrame`s produced by the online runner to a queryable store, and provide PIT-correct join helpers for training/backtesting. Two physical layers:

1. **Online store (Redis)** — last-known feature values per (symbol, freq), TTL'd; serves agent inference at <10 ms p99.
2. **Offline store (Timescale)** — append-only `features` hypertable. Long retention. Used for training, backtesting, attribution, and PIT joins with bars.

Critical invariant: a backtest joining bars at time T with features must NEVER see a feature value whose `ts_event > T`. Enforced by the PIT join.

## Files to create

```
services/features/src/features/
├── store.py              # online (Redis) writer + reader; offline (Timescale) writer
├── pit.py                # PIT join helpers
└── offline.py            # batch backfill: replay bars from Timescale, compute features, write back

libs/fincept-db/src/fincept_db/
├── features.py           # ORM model + read/write helpers for the features table
└── migrations/versions/0002_features.py   # alembic migration

services/features/tests/
├── test_store.py
├── test_pit.py
└── test_backfill.py
```

## DB model

```python
# libs/fincept-db/src/fincept_db/models.py — extend
class Feature(Base):
    __tablename__ = "features"
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    freq: Mapped[str] = mapped_column(String(8), primary_key=True)
    ts_event: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    values: Mapped[dict] = mapped_column(JSON)        # name → float | null
    tags: Mapped[dict] = mapped_column(JSON, default=dict)
```

```python
# 0002_features.py
def upgrade() -> None:
    op.create_table("features", ...)
    op.execute("SELECT create_hypertable('features', 'ts_event', chunk_time_interval => 86400000000000)")
    op.execute("CREATE INDEX ix_features_sym_freq ON features (symbol, freq, ts_event DESC)")
    op.execute("ALTER TABLE features SET (timescaledb.compress, timescaledb.compress_segmentby = 'symbol, freq')")
    op.execute("SELECT add_compression_policy('features', INTERVAL '14 days')")
```

## Contracts

### `store.py`

```python
from typing import Iterable
import orjson
from redis.asyncio import Redis
from sqlalchemy.dialects.postgresql import insert as pg_insert
from fincept_core.config import get_settings
from fincept_core.schemas import FeatureFrame
from fincept_db.engine import session_scope
from fincept_db.features import Feature  # re-export from models
from fincept_db.models import Feature as FeatureModel

# Online layer (Redis)
ONLINE_KEY = "features:{symbol}:{freq}"   # → JSON FeatureFrame, TTL = 5 days

class OnlineStore:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def put(self, ff: FeatureFrame) -> None:
        key = ONLINE_KEY.format(symbol=ff.symbol, freq=ff.freq)
        await self.redis.set(key, ff.model_dump_json(), ex=5 * 86400)

    async def get_latest(self, symbol: str, freq: str = "1m") -> FeatureFrame | None:
        v = await self.redis.get(ONLINE_KEY.format(symbol=symbol, freq=freq))
        return FeatureFrame.model_validate_json(v) if v else None

# Offline layer (Timescale)
class OfflineStore:
    async def put_many(self, frames: Iterable[FeatureFrame]) -> int:
        rows = [
            dict(symbol=f.symbol, freq=f.freq, ts_event=f.ts_event,
                 values=f.values, tags=f.tags)
            for f in frames
        ]
        if not rows:
            return 0
        async with session_scope() as s:
            stmt = pg_insert(FeatureModel).values(rows).on_conflict_do_update(
                index_elements=["symbol", "freq", "ts_event"],
                set_={"values": pg_insert(FeatureModel).excluded.values,
                      "tags": pg_insert(FeatureModel).excluded.tags},
            )
            res = await s.execute(stmt)
            return res.rowcount or 0

    async def read_range(self, symbol: str, freq: str, start_ns: int, end_ns: int) -> list[FeatureFrame]:
        from sqlalchemy import select
        async with session_scope() as s:
            q = (select(FeatureModel)
                 .where(FeatureModel.symbol == symbol,
                        FeatureModel.freq == freq,
                        FeatureModel.ts_event >= start_ns,
                        FeatureModel.ts_event < end_ns)
                 .order_by(FeatureModel.ts_event))
            rows = (await s.execute(q)).scalars().all()
            return [
                FeatureFrame(symbol=r.symbol, freq=r.freq, ts_event=r.ts_event,
                             values=r.values, tags=r.tags)
                for r in rows
            ]
```

### `pit.py` — point-in-time joins

```python
from typing import Sequence
from fincept_core.schemas import BarEvent, FeatureFrame
from .store import OfflineStore

class PITJoiner:
    """Join bars at time T with features whose ts_event <= T (per symbol+freq).
    Strictly forbids leakage."""

    def __init__(self, store: OfflineStore) -> None:
        self.store = store

    async def join_bars(self, bars: Sequence[BarEvent]) -> list[tuple[BarEvent, FeatureFrame | None]]:
        """For each bar, return the latest FeatureFrame with ts_event <= bar.ts_event."""
        out: list[tuple[BarEvent, FeatureFrame | None]] = []
        if not bars:
            return out
        # Group by (symbol, freq) and fetch a single range per group
        from collections import defaultdict
        groups: dict[tuple[str, str], list[BarEvent]] = defaultdict(list)
        for b in bars:
            groups[(b.symbol, b.freq)].append(b)
        for (sym, freq), bs in groups.items():
            start = min(b.ts_event for b in bs) - 365 * 86400 * 1_000_000_000  # 1y back
            end = max(b.ts_event for b in bs) + 1
            feats = await self.store.read_range(sym, freq, start, end)
            # PIT match: for each bar, take last feat with ts_event <= bar.ts_event
            i = 0
            for b in bs:
                while i < len(feats) and feats[i].ts_event <= b.ts_event:
                    i += 1
                latest = feats[i - 1] if i > 0 else None
                # CRITICAL invariant
                if latest is not None and latest.ts_event > b.ts_event:
                    raise RuntimeError(f"PIT violation: feature ts {latest.ts_event} > bar ts {b.ts_event}")
                out.append((b, latest))
        return out
```

### `offline.py` — batch backfill

```python
import asyncio
from fincept_core.clock import now_ns
from fincept_core.logging import get_logger
from fincept_core.schemas import FeatureFrame
from fincept_db.bars import read_bars
from .store import OfflineStore
from .transforms.price import PriceFeatures
from .transforms.volatility import VolatilityFeatures
from .transforms.cross import CrossFeatures

log = get_logger(__name__)

async def backfill(symbols: list[str], freq: str, start_ns: int, end_ns: int,
                   benchmark: str = "BTC-USD") -> int:
    """Re-compute features over a historical window. Bit-identical to online runner."""
    store = OfflineStore()
    cross = CrossFeatures(benchmark_symbol=benchmark)
    written = 0
    # Process benchmark first so its returns feed cross-features
    sym_order = [benchmark] + [s for s in symbols if s != benchmark]
    for sym in sym_order:
        pf = PriceFeatures()
        vf = VolatilityFeatures()
        bars = await read_bars(sym, freq, start_ns, end_ns)
        frames: list[FeatureFrame] = []
        for b in bars:
            pv = pf.update(b.close)
            vv = vf.update(b.open, b.high, b.low, b.close, pv.get("ret_log_1"))
            if sym == benchmark:
                cross.on_benchmark_ret(pv.get("ret_log_1"))
            cv = cross.on_symbol_ret(sym, pv.get("ret_log_1"))
            frames.append(FeatureFrame(symbol=sym, ts_event=b.ts_event, freq=freq,
                                       values={**pv, **vv, **cv}))
        n = await store.put_many(frames)
        written += n
        log.info("features.backfill.symbol", symbol=sym, rows=n)
    return written
```

## Tests

### `tests/test_pit.py`

```python
import pytest
from decimal import Decimal
from fincept_core.schemas import BarEvent, FeatureFrame, Venue, AssetClass
from features.store import OfflineStore
from features.pit import PITJoiner

@pytest.mark.asyncio
async def test_pit_no_lookahead():
    store = OfflineStore()
    # Insert features at t=10 and t=20
    await store.put_many([
        FeatureFrame(symbol="X", freq="1m", ts_event=10, values={"x": 1.0}),
        FeatureFrame(symbol="X", freq="1m", ts_event=20, values={"x": 2.0}),
    ])
    joiner = PITJoiner(store)
    bar = BarEvent(
        venue=Venue.BINANCE, symbol="X", asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=15, ts_recv=15, freq="1m",
        open=Decimal(1), high=Decimal(1), low=Decimal(1), close=Decimal(1),
        volume=Decimal(0), trades=0,
    )
    out = await joiner.join_bars([bar])
    assert out[0][1] is not None
    assert out[0][1].values["x"] == 1.0     # MUST be the t=10 feature, not the t=20 feature
```

### `tests/test_backfill.py`

```python
import pytest
from decimal import Decimal
from datetime import datetime, timezone
from features.offline import backfill
from features.store import OfflineStore

@pytest.mark.asyncio
async def test_backfill_idempotent_and_bit_identical(populated_bars):
    n1 = await backfill(["BTC-USD"], "1m", 0, 9_999_999_999_999_999_999)
    n2 = await backfill(["BTC-USD"], "1m", 0, 9_999_999_999_999_999_999)
    assert n2 >= 1                              # ON CONFLICT updates rows; rowcount > 0 is fine
    s = OfflineStore()
    out1 = await s.read_range("BTC-USD", "1m", 0, 9_999_999_999_999_999_999)
    # Re-running with the same input data must produce identical feature values
    await backfill(["BTC-USD"], "1m", 0, 9_999_999_999_999_999_999)
    out2 = await s.read_range("BTC-USD", "1m", 0, 9_999_999_999_999_999_999)
    assert [f.values for f in out1] == [f.values for f in out2]
```

## Landmines

- **PIT is the entire point of this task.** If you cannot prove no-lookahead in tests, the rest of the platform is unsafe. The test `test_pit_no_lookahead` MUST pass.
- **Online vs offline drift:** the online runner and the offline backfill MUST share the SAME `transforms/` code path. Do NOT duplicate the formulas. If you find yourself copying math between online + offline, refactor first.
- **`ON CONFLICT DO UPDATE` semantics:** re-running backfill replaces feature values for the same (symbol, freq, ts_event). This is correct — it lets you fix a bug in `volatility.py` and re-run. But it also means a buggy backfill silently overwrites good data. Always backfill into a sandbox schema first; promote when validated.
- **Backfill order matters:** the benchmark must be processed before other symbols, because `CrossFeatures` shares state. Document this in code; tests should fail if order is wrong.
- **Hypertable chunk size:** features are denser than bars (one row per bar per symbol). Re-evaluate `chunk_time_interval` in TASK-070 with profiling data.
- **`values` JSON column performance:** generic JSON queries are slow. If a specific feature column becomes hot, materialize it as its own column (separate migration; gate behind benchmarking).
- **Online TTL choice (5 days):** long enough to survive most outages, short enough to bound Redis memory. Reconsider in Phase H.

## Out of scope

- Online-store reads at <1 ms (cache locality, server-side scripting) — Phase H if profiling demands it.
- Distributed offline computation (Spark/Ray) — single-node Postgres for v1; revisit at TASK-093 if alt-data scale demands it.
- Schema evolution of `FeatureFrame.values` keys — for now, adding a new feature is non-breaking (consumers handle missing keys); removing one is breaking and requires a migration plan.

## Done when

- [ ] `features` table + hypertable created (alembic migration `0002_features.py` applies cleanly)
- [ ] `OnlineStore.put` + `get_latest` round-trip via Redis
- [ ] `OfflineStore.put_many` + `read_range` round-trip via Timescale, idempotent on re-run
- [ ] `PITJoiner.join_bars` raises if a join would leak; `test_pit_no_lookahead` passes
- [ ] `backfill` reproduces the online stream's values bit-identically on the same input
- [ ] `pytest services/features/tests` is green
- [ ] **Phase D checkpoint** met: 24-hr soak with 5 crypto pairs, no dropped messages, online store p99 < 10 ms, offline backfill bit-identical to live
