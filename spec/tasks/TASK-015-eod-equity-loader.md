# TASK-015 · EOD equity loader (yfinance → bars_1d)

**Phase:** D · **Depends on:** TASK-004 · **Blocks:** Equity strategies in Phase A+

## Goal

Daily scheduled job that fetches end-of-day OHLCV bars for all symbols in the equity universe (`AssetClass.EQUITY`) and writes them to `bars` (freq=`1d`). Sources: `yfinance` (free, default) with `polygon-api-client` as a paid alternative when `POLYGON_API_KEY` is set. Idempotent: re-running the same day is a no-op or replaces with the same values.

## Files to create

```
services/ingestor/src/ingestor/eod_equity.py
services/jobs/src/jobs/daily_eod_load.py        # APScheduler job that calls into the loader
services/ingestor/tests/test_eod_equity.py
```

## Contracts

### `eod_equity.py`

```python
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Sequence
import yfinance as yf
from fincept_core.clock import iso_to_ns
from fincept_core.config import get_settings
from fincept_core.logging import get_logger
from fincept_core.schemas import BarEvent, Venue, AssetClass
from fincept_db.bars import write_bars
from fincept_db.engine import session_scope
from fincept_db.models import UniverseSymbol
from sqlalchemy import select

log = get_logger(__name__)

class YFinanceLoader:
    """Default loader. Free, lightly rate-limited; respect ~2k req/h."""

    venue: Venue = Venue.NASDAQ  # placeholder venue for EOD; actual exchange not always known

    async def load_for_date_range(self, symbols: Sequence[str], start: date, end: date) -> int:
        """Bulk-download N symbols × M dates. Returns row count written."""
        # yfinance is sync; run in a thread to keep the loop free.
        import asyncio
        def _fetch() -> list[BarEvent]:
            data = yf.download(
                tickers=list(symbols),
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                interval="1d",
                group_by="ticker",
                auto_adjust=False,         # raw OHLC; agent layer handles adjustments
                actions=False,
                progress=False,
                threads=False,
            )
            out: list[BarEvent] = []
            for sym in symbols:
                df = data[sym] if len(symbols) > 1 else data
                if df.empty:
                    log.warning("eod.empty", symbol=sym)
                    continue
                for ts, row in df.iterrows():
                    if any(p is None for p in (row.get("Open"), row.get("Close"), row.get("Volume"))):
                        continue
                    ev = BarEvent(
                        venue=self.venue,
                        symbol=sym,
                        asset_class=AssetClass.EQUITY,
                        ts_event=iso_to_ns(ts.isoformat()),
                        ts_recv=iso_to_ns(ts.isoformat()),
                        freq="1d",
                        open=Decimal(str(row["Open"])),
                        high=Decimal(str(row["High"])),
                        low=Decimal(str(row["Low"])),
                        close=Decimal(str(row["Close"])),
                        volume=Decimal(str(int(row["Volume"]))),
                        trades=0,
                        vwap=None,
                    )
                    out.append(ev)
            return out
        bars = await asyncio.to_thread(_fetch)
        written = await write_bars(bars)
        log.info("eod.loaded", source="yfinance", symbols=len(symbols),
                 days=(end - start).days, rows=written)
        return written

class PolygonLoader:
    """Paid; activate iff POLYGON_API_KEY is set. Only stub; full impl when budget allows."""
    async def load_for_date_range(self, symbols: Sequence[str], start: date, end: date) -> int:
        raise NotImplementedError("PolygonLoader: enable in Phase H if budget approved")

def get_loader() -> "YFinanceLoader | PolygonLoader":
    if get_settings().polygon_api_key:
        return PolygonLoader()
    return YFinanceLoader()

async def get_equity_universe() -> list[str]:
    async with session_scope() as s:
        q = select(UniverseSymbol).where(
            UniverseSymbol.asset_class == AssetClass.EQUITY.value,
            UniverseSymbol.active == True,  # noqa: E712
        )
        rows = (await s.execute(q)).scalars().all()
        return [r.symbol for r in rows]
```

### `services/jobs/src/jobs/daily_eod_load.py`

```python
from datetime import date, timedelta
from fincept_core.logging import get_logger
from ingestor.eod_equity import get_loader, get_equity_universe

log = get_logger(__name__)

async def run_daily(target: date | None = None) -> None:
    """Scheduled at 22:30 ET on US trading days. APScheduler triggers via main.py."""
    if target is None:
        target = date.today() - timedelta(days=1)  # previous trading day
    universe = await get_equity_universe()
    if not universe:
        log.warning("eod.empty_universe")
        return
    loader = get_loader()
    n = await loader.load_for_date_range(universe, target, target)
    log.info("eod.run.complete", target=target.isoformat(), rows=n)
```

## Tests

### `tests/test_eod_equity.py`

```python
import pytest
from datetime import date
from decimal import Decimal
from fincept_db.bars import read_bars
from ingestor.eod_equity import YFinanceLoader

@pytest.mark.live  # requires internet; skipped in default CI
@pytest.mark.asyncio
async def test_yfinance_load_aapl_range():
    loader = YFinanceLoader()
    n = await loader.load_for_date_range(["AAPL"], date(2024, 11, 1), date(2024, 11, 5))
    assert n >= 3                                         # at least 3 trading days in that range
    bars = await read_bars("AAPL", "1d", 0, 9_999_999_999_999_999_999)
    assert any(b.symbol == "AAPL" and b.close > Decimal(0) for b in bars)

@pytest.mark.asyncio
async def test_yfinance_idempotent(monkeypatch):
    """Two consecutive runs over the same range produce same row count or 0 (depending on conflict policy)."""
    # Mock yfinance so we don't hit the network; verify write_bars is called twice with same data.
    ...
```

## Landmines

- **`yfinance` is best-effort:** it can return partial / missing days, sometimes silently. Always log the fetched-row count vs expected and alert if < 95%.
- **Adjusted vs raw close:** `auto_adjust=False` returns RAW OHLC; agents that depend on splits/dividends must apply adjustments themselves (or query Polygon if the budget is approved). Document this clearly. Otherwise backtests pre-split look discontinuous.
- **Survivorship bias:** the universe table only contains currently-active symbols. For backtesting, you need a survivorship-bias-free universe history — but that's a Phase X+ task (TASK-093 alt-data) or sourced separately. v1 of EOD load uses current universe only.
- **Holiday calendar:** the scheduler must skip non-trading days. Use `pandas_market_calendars` to compute. Otherwise weekend runs return empty.
- **Time zones:** EOD bars are at the close of the LISTING exchange (mostly NYSE/NASDAQ ET). Store ts_event as the close timestamp in UTC ns. Do NOT mix in your local time.
- **Polygon rate limits:** at lowest tier (5 req/min), bulk download is slow. Batch by date range, not per-symbol. Stub for now; flesh out if/when paid tier is approved.

## Out of scope

- Intraday equity bars (1m / 1h) — Phase X+ if needed.
- Corporate actions / splits / dividends — `services/jobs/corporate_actions.py` is a separate task (not yet specified).
- Options EOD — Phase X+ via TASK-080 / TASK-100.
- Alternative-data EOD (10-K filings, earnings dates) — TASK-082 (insider/short) or TASK-093 (alt-data).

## Done when

- [ ] `eod_equity.py`, `daily_eod_load.py`, and the test file exist
- [ ] `YFinanceLoader.load_for_date_range` round-trips via `bars` table (verified via integration test, when `pytest -m live` is run)
- [ ] Idempotency confirmed: re-running same date produces no duplicate rows (ON CONFLICT in `bars.write_bars` from TASK-004)
- [ ] APScheduler invokes `run_daily` at 22:30 ET on weekdays (configured in `services/jobs/main.py`)
- [ ] `mypy services/ingestor` and `mypy services/jobs` are green
- [ ] Logging includes source, symbol count, day range, and row count for every run
