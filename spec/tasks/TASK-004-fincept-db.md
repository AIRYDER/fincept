# TASK-004 · `fincept-db` — async SQLAlchemy + Timescale + audit log

**Phase:** F · **Depends on:** TASK-002 · **Blocks:** TASK-010, TASK-017, TASK-020, TASK-044, TASK-045, TASK-050

## Goal

Async SQLAlchemy engine, ORM models, alembic migrations, and access helpers for ticks, bars, and the WORM audit log. Timescale hypertables for `trades`, `book_deltas`, `bars_1m`, `bars_1h`, `bars_1d`. Plain Postgres tables for `audit_log`, `strategies`, `universe`.

## Files to create

```
libs/fincept-db/
├── pyproject.toml
├── alembic.ini
├── src/fincept_db/
│   ├── __init__.py
│   ├── engine.py             # async engine factory, session helpers
│   ├── models.py             # ORM (DeclarativeBase) — all tables
│   ├── ticks.py              # writes/reads for trades + book_deltas
│   ├── bars.py               # writes/reads for bars_*
│   ├── audit.py              # append-only audit log writer + reader
│   └── migrations/
│       ├── env.py            # alembic env (async)
│       ├── script.py.mako
│       └── versions/
│           └── 0001_initial.py  # initial schema with hypertables
└── tests/
    ├── conftest.py           # ephemeral test DB fixture
    ├── test_ticks.py
    ├── test_bars.py
    └── test_audit.py
```

## `pyproject.toml`

```toml
[project]
name = "fincept-db"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "sqlalchemy[asyncio]>=2.0.36",
    "asyncpg>=0.30",
    "alembic>=1.14",
    "fincept-core",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/fincept_db"]
```

## Contracts (MUST match)

### `engine.py`

```python
from typing import AsyncIterator
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from fincept_core.config import get_settings

_engine: AsyncEngine | None = None

def get_engine() -> AsyncEngine:
    """Lazy singleton engine. Reads DATABASE_URL from settings."""
    global _engine
    if _engine is None:
        url = get_settings().database_url
        _engine = create_async_engine(url, pool_size=20, max_overflow=10, pool_pre_ping=True)
    return _engine

_SessionLocal: async_sessionmaker[AsyncSession] | None = None

def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _SessionLocal

@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional session context. Commits on exit; rolls back on exception."""
    sm = get_sessionmaker()
    async with sm() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise
```

### `models.py`

```python
from decimal import Decimal
from datetime import datetime
from sqlalchemy import BigInteger, Index, Integer, Numeric, String, Boolean, JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    pass

class Trade(Base):
    __tablename__ = "trades"
    venue: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts_event: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # ns; hypertable time column
    seq: Mapped[int | None] = mapped_column(BigInteger, primary_key=True, default=0)
    asset_class: Mapped[str] = mapped_column(String(16))
    ts_recv: Mapped[int] = mapped_column(BigInteger)
    price: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    size: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    side: Mapped[str | None] = mapped_column(String(4))
    __table_args__ = (
        Index("ix_trades_symbol_ts", "symbol", "ts_event"),
    )

class Bar(Base):
    """Single table for 1m / 1h / 1d. Distinguished by `freq`."""
    __tablename__ = "bars"
    venue: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    freq: Mapped[str] = mapped_column(String(8), primary_key=True)  # "1m", "1h", "1d"
    ts_event: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(16))
    open: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    high: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    low: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    close: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    volume: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    trades: Mapped[int] = mapped_column(Integer)
    vwap: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))

class BookDelta(Base):
    __tablename__ = "book_deltas"
    venue: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts_event: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(16))
    ts_recv: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSON)  # {bids_add, bids_remove, asks_add, asks_remove}

class AuditLog(Base):
    """Append-only event log. WORM. Never UPDATE or DELETE."""
    __tablename__ = "audit_log"
    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)  # ULID
    ts_event: Mapped[int] = mapped_column(BigInteger)
    actor: Mapped[str] = mapped_column(String(64))                       # "orchestrator", "risk", ...
    event_type: Mapped[str] = mapped_column(String(64))                  # "decision", "risk_check", "fill", ...
    correlation_id: Mapped[str | None] = mapped_column(String(64))       # decision_id or order_id
    payload: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (
        Index("ix_audit_corr", "correlation_id"),
        Index("ix_audit_ts", "ts_event"),
    )

class Strategy(Base):
    __tablename__ = "strategies"
    strategy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[int] = mapped_column(BigInteger)

class UniverseSymbol(Base):
    __tablename__ = "universe"
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(16))
    venue_default: Mapped[str] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
```

### `ticks.py`

```python
from decimal import Decimal
from typing import Iterable, AsyncIterator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from fincept_core.schemas import TradeEvent, Venue, AssetClass
from .engine import session_scope
from .models import Trade

async def write_trades(events: Iterable[TradeEvent]) -> int:
    """Bulk-insert trades. Returns row count. Idempotent on (venue, symbol, ts_event, seq)."""
    rows = [
        dict(
            venue=e.venue.value, symbol=e.symbol, ts_event=e.ts_event,
            seq=e.seq or 0, asset_class=e.asset_class.value, ts_recv=e.ts_recv,
            price=e.price, size=e.size, side=e.side.value if e.side else None,
        )
        for e in events
    ]
    if not rows:
        return 0
    async with session_scope() as s:
        stmt = pg_insert(Trade).values(rows).on_conflict_do_nothing()
        result = await s.execute(stmt)
        return result.rowcount or 0

async def read_trades(symbol: str, start_ns: int, end_ns: int, venue: str | None = None) -> list[TradeEvent]:
    """Range query, ordered by ts_event ASC. PIT-safe: caller passes explicit start/end."""
    async with session_scope() as s:
        q = select(Trade).where(Trade.symbol == symbol, Trade.ts_event >= start_ns, Trade.ts_event < end_ns)
        if venue:
            q = q.where(Trade.venue == venue)
        q = q.order_by(Trade.ts_event, Trade.seq)
        rows = (await s.execute(q)).scalars().all()
        return [
            TradeEvent(
                venue=Venue(r.venue), symbol=r.symbol, asset_class=AssetClass(r.asset_class),
                ts_event=r.ts_event, ts_recv=r.ts_recv, seq=r.seq,
                price=Decimal(r.price), size=Decimal(r.size),
            )
            for r in rows
        ]
```

### `bars.py`

```python
# Mirrors ticks.py: write_bars(BarEvent iterable), read_bars(symbol, freq, start_ns, end_ns).
# Use ON CONFLICT (venue, symbol, freq, ts_event) DO UPDATE for idempotent re-ingestion of EOD revisions.
```

### `audit.py`

```python
from typing import Any
from sqlalchemy.dialects.postgresql import insert as pg_insert
from fincept_core.ids import new_id
from fincept_core.clock import now_ns
from .engine import session_scope
from .models import AuditLog

async def append(actor: str, event_type: str, payload: dict[str, Any], correlation_id: str | None = None) -> str:
    """Append to audit log. Returns event_id. NEVER updates or deletes."""
    eid = new_id()
    async with session_scope() as s:
        stmt = pg_insert(AuditLog).values(
            event_id=eid, ts_event=now_ns(), actor=actor, event_type=event_type,
            correlation_id=correlation_id, payload=payload,
        ).on_conflict_do_nothing()  # event_id collision = no-op
        await s.execute(stmt)
    return eid
```

### `migrations/versions/0001_initial.py`

Alembic migration creates all tables AND the Timescale hypertables. Critical step:

```python
def upgrade() -> None:
    # Create tables (auto from metadata)
    op.create_table("trades", ...)  # full column list per models.py
    op.create_table("bars", ...)
    op.create_table("book_deltas", ...)
    op.create_table("audit_log", ...)
    op.create_table("strategies", ...)
    op.create_table("universe", ...)

    # Convert time-series tables to Timescale hypertables
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.execute("SELECT create_hypertable('trades', 'ts_event', chunk_time_interval => 86400000000000)")  # 1 day in ns
    op.execute("SELECT create_hypertable('bars', 'ts_event', chunk_time_interval => 86400000000000)")
    op.execute("SELECT create_hypertable('book_deltas', 'ts_event', chunk_time_interval => 3600000000000)")  # 1 hour

    # Compression policy (older than 7 days)
    op.execute("ALTER TABLE trades SET (timescaledb.compress, timescaledb.compress_segmentby = 'venue, symbol')")
    op.execute("SELECT add_compression_policy('trades', INTERVAL '7 days')")
    # ... same for bars, book_deltas

    # Retention (older than 90 days for ticks; bars retained indefinitely)
    op.execute("SELECT add_retention_policy('book_deltas', INTERVAL '30 days')")
```

## Tests (MUST pass)

### `tests/conftest.py`

Spins up an ephemeral schema in the configured test DB (`DATABASE_URL` overridden in CI to a Timescale-enabled instance). Drop on teardown.

```python
import pytest_asyncio
from fincept_db.engine import get_engine
from fincept_db.models import Base

@pytest_asyncio.fixture(scope="session", autouse=True)
async def _schema():
    eng = get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
```

### `tests/test_ticks.py`

```python
import pytest
from decimal import Decimal
from fincept_core.schemas import TradeEvent, Venue, AssetClass
from fincept_db.ticks import write_trades, read_trades

@pytest.mark.asyncio
async def test_write_and_read_trades_roundtrip():
    evs = [TradeEvent(
        venue=Venue.BINANCE, symbol="BTC-USD", asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=t, ts_recv=t+1, seq=t, price=Decimal("100.5"), size=Decimal("0.1"),
    ) for t in (1_000_000_000, 2_000_000_000, 3_000_000_000)]
    n = await write_trades(evs)
    assert n == 3
    out = await read_trades("BTC-USD", 0, 4_000_000_000)
    assert len(out) == 3
    assert out[0].price == Decimal("100.5")

@pytest.mark.asyncio
async def test_write_trades_idempotent_on_pkey():
    ev = TradeEvent(venue=Venue.BINANCE, symbol="ETH-USD", asset_class=AssetClass.CRYPTO_SPOT,
                    ts_event=10, ts_recv=11, seq=1, price=Decimal("2000"), size=Decimal("0.5"))
    await write_trades([ev])
    n2 = await write_trades([ev])  # duplicate
    assert n2 == 0
```

### `tests/test_audit.py`

```python
import pytest
from fincept_db import audit

@pytest.mark.asyncio
async def test_audit_append_returns_id():
    eid = await audit.append("orchestrator", "decision", {"strategy_id": "s1"}, correlation_id="d1")
    assert len(eid) == 26  # ULID length
```

## Out of scope

- No read replica routing (defer to Phase H).
- No partitioning beyond Timescale's hypertable chunking.
- No connection pool tuning beyond defaults; revisit at TASK-070 with profiling data.
- No archival to object storage (TASK-074 in Phase H).

## Done when

- [ ] Files exist
- [ ] `alembic upgrade head` succeeds against a Timescale-enabled Postgres
- [ ] `pytest libs/fincept-db/tests` is green (requires running DB; `make dev` provides one)
- [ ] `mypy libs/fincept-db` is green
- [ ] `ruff check libs/fincept-db` is green
- [ ] Hypertables verified via `SELECT * FROM timescaledb_information.hypertables`
