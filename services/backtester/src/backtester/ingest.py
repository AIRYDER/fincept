"""
backtester.ingest — pure conversion of vendor bar payloads to the
canonical parquet schema the runner consumes.

Three small public functions, all sync, all deterministic, no network:

  - :func:`parse_alpaca_bars_payload`   Alpaca ``/v2/stocks/bars`` JSON ->
                                        ``list[BarEvent]``
  - :func:`parse_yfinance_daily_frame`  one yfinance per-symbol DataFrame
                                        (daily) -> ``list[BarEvent]``
  - :func:`bars_to_parquet`             ``list[BarEvent]`` -> parquet
                                        with the runner's required schema
                                        (``symbol, ts_event, open, high,
                                        low, close, volume`` + optional
                                        ``vwap, trades``)

Network code lives in ``scripts/ingest_bars.py`` so this module stays
trivially testable: pass in a hand-crafted dict / DataFrame, assert on
the resulting ``BarEvent``\\s or the parquet contents.

Daily yfinance bars are pinned to the NYSE close (16:00 America/New_York,
DST-aware) — same convention as ``ingestor.eod_equity`` so the two
sources produce comparable timestamps.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from fincept_core.schemas import AssetClass, BarEvent, Venue

_NYSE_TZ = ZoneInfo("America/New_York")
_NYSE_CLOSE_LOCAL = time(16, 0)

_REQUIRED_PARQUET_COLS = (
    "symbol",
    "ts_event",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


# --------------------------------------------------------------------------- #
# Time helpers                                                                #
# --------------------------------------------------------------------------- #


def _iso_to_ns(value: str) -> int:
    """Parse Alpaca's ISO-8601 timestamps (``...Z`` or ``...+00:00``) to
    UTC nanoseconds.  Raises ``ValueError`` on unparseable input — the
    caller is expected to skip the row."""
    iso = value
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1_000_000_000)


def _trading_day_close_to_ns(d: date) -> int:
    """NYSE close (16:00 ET) on date *d* expressed in UTC ns; DST aware."""
    close_ny = datetime.combine(d, _NYSE_CLOSE_LOCAL, tzinfo=_NYSE_TZ)
    return int(close_ny.timestamp() * 1_000_000_000)


# --------------------------------------------------------------------------- #
# Alpaca parser                                                               #
# --------------------------------------------------------------------------- #


def parse_alpaca_bars_payload(
    payload: dict[str, Any],
    *,
    venue: Venue = Venue.NASDAQ,
    asset_class: AssetClass = AssetClass.EQUITY,
    freq: str = "1m",
) -> list[BarEvent]:
    """Parse Alpaca's ``/v2/stocks/bars`` response into ``BarEvent``\\s.

    Expected shape::

        {"bars": {"AAPL": [{"t": "...Z", "o": ..., "h": ..., "l": ...,
                            "c": ..., "v": ..., "n": ..., "vw": ...}]},
         "next_page_token": null}

    Bars whose ``t`` is unparseable or whose OHLCV is missing are
    silently dropped (vendor occasionally emits these on halts) so a
    single bad row doesn't break the whole ingest.  Order is preserved:
    Alpaca returns oldest-first within each symbol's array.
    """
    bars_field = payload.get("bars") or {}
    if not isinstance(bars_field, dict):
        return []
    out: list[BarEvent] = []
    for symbol, raw_bars in bars_field.items():
        if not isinstance(raw_bars, list):
            continue
        for raw in raw_bars:
            if not isinstance(raw, dict):
                continue
            try:
                ts = _iso_to_ns(str(raw["t"]))
                open_ = Decimal(str(raw["o"]))
                high = Decimal(str(raw["h"]))
                low = Decimal(str(raw["l"]))
                close = Decimal(str(raw["c"]))
                volume = Decimal(str(raw["v"]))
            except (KeyError, ValueError, TypeError):
                continue
            vwap = None
            if raw.get("vw") is not None:
                try:
                    vwap = Decimal(str(raw["vw"]))
                except (ValueError, TypeError):
                    vwap = None
            trades_raw = raw.get("n") or 0
            try:
                trades = int(trades_raw)
            except (ValueError, TypeError):
                trades = 0
            out.append(
                BarEvent(
                    venue=venue,
                    symbol=str(symbol),
                    asset_class=asset_class,
                    ts_event=ts,
                    ts_recv=ts,
                    freq=freq,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    trades=trades,
                    vwap=vwap,
                )
            )
    return out


# --------------------------------------------------------------------------- #
# yfinance daily parser                                                       #
# --------------------------------------------------------------------------- #


def parse_yfinance_daily_frame(
    symbol: str,
    rows: Iterable[dict[str, Any]],
    *,
    venue: Venue = Venue.NASDAQ,
    asset_class: AssetClass = AssetClass.EQUITY,
) -> list[BarEvent]:
    """Convert a sequence of ``{date, Open, High, Low, Close, Volume}``
    dicts (the shape produced by ``DataFrame.reset_index().to_dict(...)``)
    into ``BarEvent``\\s pinned to the NYSE close.

    Rows missing any OHLCV field, or carrying NaN-ish values, are
    dropped.  ``date`` may be a ``datetime.date``, ``datetime``, or
    pandas Timestamp — anything with a ``.date()`` accessor or that
    coerces via ``date.fromisoformat`` works.
    """
    out: list[BarEvent] = []
    for row in rows:
        date_value = row.get("Date") or row.get("date") or row.get("ts") or row.get("index")
        if date_value is None:
            continue
        try:
            d = _coerce_date(date_value)
        except (TypeError, ValueError):
            continue
        try:
            open_ = _row_decimal(row, "Open")
            high = _row_decimal(row, "High")
            low = _row_decimal(row, "Low")
            close = _row_decimal(row, "Close")
            volume = _row_decimal(row, "Volume")
        except (KeyError, ValueError, TypeError):
            continue
        ns = _trading_day_close_to_ns(d)
        out.append(
            BarEvent(
                venue=venue,
                symbol=symbol,
                asset_class=asset_class,
                ts_event=ns,
                ts_recv=ns,
                freq="1d",
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
                trades=0,
                vwap=None,
            )
        )
    return out


def _coerce_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "date") and callable(value.date):
        coerced = value.date()
        if isinstance(coerced, date):
            return coerced
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise TypeError(f"cannot coerce {type(value).__name__} -> date")


def _row_decimal(row: dict[str, Any], key: str) -> Decimal:
    """Extract ``key`` from row as Decimal; raise on missing/NaN."""
    value = row.get(key)
    if value is None:
        raise KeyError(key)
    # NaN check that doesn't require pandas: NaN != NaN.
    if isinstance(value, float) and value != value:
        raise ValueError(f"{key} is NaN")
    return Decimal(str(value))


# --------------------------------------------------------------------------- #
# Parquet writer                                                              #
# --------------------------------------------------------------------------- #


def bars_to_parquet(
    bars: Sequence[BarEvent],
    out: pathlib.Path | str,
) -> int:
    """Write *bars* to *out* in the runner's expected schema.

    Returns the number of rows written.  Sorts by ``(ts_event, symbol)``
    so a downstream merge of multiple files stays deterministic.  Stores
    Decimal fields as float64 — the runner re-coerces to Decimal on
    read, and float64 has enough precision for OHLCV at typical equity /
    crypto magnitudes (~15 sig figs).  If you need full Decimal
    precision end-to-end, switch to a Decimal128 column type, but be
    aware that Polars Decimal support is still experimental on read.
    """
    if not bars:
        path = pathlib.Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        empty = pl.DataFrame(
            schema={
                "symbol": pl.Utf8,
                "ts_event": pl.Int64,
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Float64,
                "trades": pl.Int64,
                "vwap": pl.Float64,
            }
        )
        empty.write_parquet(path)
        return 0

    rows = {
        "symbol": [b.symbol for b in bars],
        "ts_event": [int(b.ts_event) for b in bars],
        "open": [float(b.open) for b in bars],
        "high": [float(b.high) for b in bars],
        "low": [float(b.low) for b in bars],
        "close": [float(b.close) for b in bars],
        "volume": [float(b.volume) for b in bars],
        "trades": [int(b.trades) for b in bars],
        "vwap": [float(b.vwap) if b.vwap is not None else None for b in bars],
    }
    df = pl.DataFrame(rows).sort(["ts_event", "symbol"])
    path = pathlib.Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)
    return df.height


def assert_parquet_matches_runner_schema(path: pathlib.Path | str) -> None:
    """Raise ``ValueError`` if *path* is missing any column the runner
    requires.  Cheap pre-flight check the CLI uses after writing."""
    df = pl.read_parquet(path)
    missing = [c for c in _REQUIRED_PARQUET_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"parquet at {path} missing required columns: {missing}")
