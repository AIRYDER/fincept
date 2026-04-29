"""
ingestor.eod_equity — daily end-of-day equity loader.

Public surface:

  - ``YFinanceLoader``         — default loader (free, lightly rate-limited)
  - ``PolygonLoader``          — paid stub (only activates when ``POLYGON_API_KEY`` is set)
  - ``Loader`` (Protocol)      — minimal interface every loader implements
  - ``get_loader()``           — factory that picks YFinance vs Polygon based on settings
  - ``get_equity_universe()``  — reads ``universe`` table for active equities
  - ``trading_day_close_to_ns(d)``  — DST-aware UTC ns for NYSE close on date *d*
  - ``is_us_trading_day(d)``        — weekday-only check (holidays handled best-effort)
  - ``_parse_yfinance_frame``       — pure helper used by ``YFinanceLoader``; exported for tests

Design notes
============

**DST-aware timestamps.**  The original spec snippet used
``iso_to_ns(ts.isoformat())`` on a tz-naive pandas Timestamp.  That silently
interprets the timestamp as *local* time (host clock), so the same code
produces different ``ts_event`` values on a developer laptop in Singapore
vs a server in NY.  Bars stored that way would not align with intraday
crypto data and would break PIT joins.  We instead pin the close to
NYSE local 16:00 via ``zoneinfo.ZoneInfo("America/New_York")``, which
gets DST right automatically.

**Dependency injection over import-time coupling.**  ``YFinanceLoader``
takes a ``download_fn`` callable (defaulting to ``yfinance.download``)
and a ``write_fn`` callable (defaulting to ``fincept_db.bars.write_bars``).
Tests inject hand-built DataFrames and capture ``write_fn`` calls in-memory
— no network, no live database, deterministic.

**Holidays.**  Beyond weekends, US market holidays (e.g., Thanksgiving) are
handled by yfinance returning empty rows; the loader logs an
``eod.empty_frame`` warning and proceeds.  Adding ``pandas_market_calendars``
for accurate per-day skipping is a follow-up.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from sqlalchemy import select

from fincept_core.config import get_settings
from fincept_core.logging import get_logger
from fincept_core.schemas import AssetClass, BarEvent, Venue
from fincept_db.bars import write_bars
from fincept_db.engine import session_scope
from fincept_db.models import UniverseSymbol

log = get_logger(__name__)

NYSE_TZ = ZoneInfo("America/New_York")
NYSE_CLOSE_LOCAL = time(16, 0)


# ---------------------------------------------------------------------------
# Trading-day helpers
# ---------------------------------------------------------------------------


def trading_day_close_to_ns(d: date) -> int:
    """Return UTC nanoseconds for the NYSE close (16:00 ET) on date *d*.

    DST-aware: an early-November date returns 21:00 UTC (EST = UTC-5);
    a July date returns 20:00 UTC (EDT = UTC-4).  This is the canonical
    ``ts_event`` for an EOD bar — the close of the listing exchange's
    trading session, expressed in our shared epoch (UTC nanoseconds).
    """
    close_ny = datetime.combine(d, NYSE_CLOSE_LOCAL, tzinfo=NYSE_TZ)
    return int(close_ny.timestamp() * 1_000_000_000)


def is_us_trading_day(d: date) -> bool:
    """Return True iff *d* is Mon-Fri.

    Holiday handling (Thanksgiving, Good Friday, etc.) is intentionally
    deferred to yfinance — it returns empty rows on holidays and the
    loader logs an ``eod.empty_frame`` warning.  When budget allows, swap
    in ``pandas_market_calendars.get_calendar("XNYS").schedule(...)``.
    """
    return d.weekday() < 5


# ---------------------------------------------------------------------------
# DataFrame parsing (pure, sync, fully testable)
# ---------------------------------------------------------------------------


_REQUIRED_COLS = ("Open", "High", "Low", "Close", "Volume")


def _parse_yfinance_frame(
    symbol: str, frame: pd.DataFrame, *, venue: Venue = Venue.NASDAQ
) -> list[BarEvent]:
    """Convert a per-symbol yfinance DataFrame into canonical ``BarEvent``s.

    Skips rows where any of OHLCV is NaN (yfinance occasionally yields
    these for halt days or partial bars).  Returns ``[]`` for an empty or
    all-NaN frame so callers can detect shortfall.
    """
    if frame is None or len(frame) == 0:
        return []
    out: list[BarEvent] = []
    for ts, row in frame.iterrows():
        raw_open = row.get("Open")
        raw_high = row.get("High")
        raw_low = row.get("Low")
        raw_close = row.get("Close")
        raw_volume = row.get("Volume")
        if any(_is_missing(v) for v in (raw_open, raw_high, raw_low, raw_close, raw_volume)):
            continue
        bar_date = _ts_to_date(ts)
        ns = trading_day_close_to_ns(bar_date)
        out.append(
            BarEvent(
                venue=venue,
                symbol=symbol,
                asset_class=AssetClass.EQUITY,
                ts_event=ns,
                ts_recv=ns,
                freq="1d",
                open=Decimal(str(raw_open)),
                high=Decimal(str(raw_high)),
                low=Decimal(str(raw_low)),
                close=Decimal(str(raw_close)),
                # int() truncates the .0 yfinance always appends to volumes;
                # cast Any silences mypy now that _is_missing has narrowed.
                volume=Decimal(str(int(cast(Any, raw_volume)))),
                trades=0,
                vwap=None,
            )
        )
    return out


def _is_missing(value: Any) -> bool:
    """True if *value* is None or NaN."""
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _ts_to_date(ts: Any) -> date:
    """Coerce a pandas/numpy timestamp index value to a Python ``date``."""
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, date):
        return ts
    # pandas.Timestamp has both .date() and is a subclass of datetime, but
    # be defensive in case of numpy datetime64 or other oddities.
    return pd.Timestamp(ts).date()


def _extract_per_symbol_frame(data: pd.DataFrame, symbol: str, num_symbols: int) -> pd.DataFrame:
    """Pull the per-symbol slice out of yfinance's variably-shaped output.

    ``yf.download`` returns:

      - For a single ticker: a flat ``DataFrame[Open, High, Low, Close, Volume]``.
      - For multiple tickers with ``group_by="ticker"``: a multi-level
        column DataFrame indexed by ``(ticker, OHLCV)``.

    This helper hides that shape difference from the parser.
    """
    if num_symbols == 1:
        return data
    if symbol in data.columns.get_level_values(0):
        # data[symbol] over a multi-level column index returns a DataFrame,
        # but mypy types it as Series; the cast keeps the runtime fast and
        # the type accurate.
        return cast(pd.DataFrame, data[symbol])
    return pd.DataFrame()


# ---------------------------------------------------------------------------
# Loader Protocol + concrete implementations
# ---------------------------------------------------------------------------


DownloadFn = Callable[..., pd.DataFrame]
WriteFn = Callable[[Sequence[BarEvent]], Any]  # async callable returning int


class Loader(Protocol):
    """The minimal interface ``run_daily`` needs from any loader."""

    venue: Venue

    async def load_for_date_range(self, symbols: Sequence[str], start: date, end: date) -> int: ...


class YFinanceLoader:
    """Default loader: free, lightly rate-limited (~2k req/h).

    Both ``download_fn`` and ``write_fn`` are injected so tests can swap
    in fakes.  Defaults are the production wiring (``yfinance.download``
    and ``fincept_db.bars.write_bars``).
    """

    venue: Venue = Venue.NASDAQ
    shortfall_threshold: float = 0.95

    def __init__(
        self,
        *,
        download_fn: DownloadFn | None = None,
        write_fn: WriteFn | None = None,
    ) -> None:
        self._download_fn = download_fn if download_fn is not None else yf.download
        self._write_fn = write_fn if write_fn is not None else write_bars

    async def load_for_date_range(self, symbols: Sequence[str], start: date, end: date) -> int:
        if not symbols:
            log.warning("eod.no_symbols")
            return 0
        if start > end:
            raise ValueError(f"start {start} is after end {end}")

        symbols_list = list(symbols)
        bars = await asyncio.to_thread(self._fetch_and_parse, symbols_list, start, end)

        expected_rows = self._expected_rows(symbols_list, start, end)
        if expected_rows > 0 and len(bars) / expected_rows < self.shortfall_threshold:
            log.warning(
                "eod.shortfall",
                symbols=len(symbols_list),
                start=start.isoformat(),
                end=end.isoformat(),
                expected=expected_rows,
                fetched=len(bars),
            )

        written = await self._write_fn(bars)
        log.info(
            "eod.loaded",
            source="yfinance",
            symbols=len(symbols_list),
            days=(end - start).days + 1,
            rows=int(written),
        )
        return int(written)

    def _fetch_and_parse(self, symbols: list[str], start: date, end: date) -> list[BarEvent]:
        # yfinance treats `end` exclusively, so we add one day to include
        # the requested end-of-range date.
        data = self._download_fn(
            tickers=symbols,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            actions=False,
            progress=False,
            threads=False,
        )
        bars: list[BarEvent] = []
        for sym in symbols:
            frame = _extract_per_symbol_frame(data, sym, len(symbols))
            parsed = _parse_yfinance_frame(sym, frame, venue=self.venue)
            if not parsed:
                log.warning("eod.empty_frame", symbol=sym)
            bars.extend(parsed)
        return bars

    @staticmethod
    def _expected_rows(symbols: list[str], start: date, end: date) -> int:
        """Approximate trading-day count times symbol count.

        We use the simple weekday count - accurate within ~9 days/year
        for US holidays, which is good enough for a shortfall heuristic
        (95 % threshold absorbs the holiday slack).
        """
        days = 0
        d = start
        while d <= end:
            if is_us_trading_day(d):
                days += 1
            d += timedelta(days=1)
        return days * len(symbols)


class PolygonLoader:
    """Paid alternative; activated when ``POLYGON_API_KEY`` is configured.

    Stub for now — full implementation is gated on Phase H budget approval
    (see TASK-015 §"Out of scope" and TASK-100).  Calling it raises so a
    misconfiguration fails loudly rather than silently no-op'ing.
    """

    venue: Venue = Venue.NASDAQ

    async def load_for_date_range(self, symbols: Sequence[str], start: date, end: date) -> int:
        raise NotImplementedError("PolygonLoader is a stub; enable in Phase H if budget approved")


def get_loader() -> Loader:
    """Pick a loader based on settings.

    Currently always returns ``YFinanceLoader``.  When the operator sets
    ``FINCEPT_POLYGON_API_KEY`` *and* the Polygon implementation is
    fleshed out, this factory will return ``PolygonLoader`` instead.
    """
    if get_settings().POLYGON_API_KEY:
        # Stub is intentionally not returned yet — we only switch when the
        # paid integration is real.  Logging here makes the gap visible.
        log.info("eod.polygon_key_set_but_loader_not_implemented")
    return YFinanceLoader()


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------


async def get_equity_universe() -> list[str]:
    """Return the active equity symbols from the ``universe`` table."""
    async with session_scope() as session:
        query = select(UniverseSymbol).where(
            UniverseSymbol.asset_class == AssetClass.EQUITY.value,
            UniverseSymbol.active.is_(True),
        )
        rows = (await session.execute(query)).scalars().all()
        return [row.symbol for row in rows]
