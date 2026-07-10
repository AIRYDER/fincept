"""
scripts/ingest_bars.py — pull historical OHLCV into the backtester's
parquet schema from either Alpaca or yfinance.

The backtester runner only reads parquet — this script is the bridge
between vendor APIs and that schema.  It is intentionally thin: all
parsing lives in :mod:`backtester.ingest`, fully unit-tested.  This
file owns the network plumbing (httpx for Alpaca, ``yf.download`` for
yfinance) and the CLI surface.

Usage::

  # Daily bars via yfinance (no credentials required)
  uv run python scripts/ingest_bars.py \\
      --source yfinance --symbols AAPL,MSFT,SPY \\
      --start 2024-01-02 --end 2024-12-31 \\
      --out data/equity_2024.parquet

  # 1-minute bars via Alpaca (paper data feed)
  $env:FINCEPT_ALPACA_API_KEY = "..."
  $env:FINCEPT_ALPACA_API_SECRET = "..."
  uv run python scripts/ingest_bars.py \\
      --source alpaca --symbols AAPL --timeframe 1Min \\
      --start 2024-07-15 --end 2024-07-19 \\
      --out data/aapl_1m_jul15-19.parquet

  # Auto: Alpaca if credentials present, else yfinance daily
  uv run python scripts/ingest_bars.py --symbols AAPL --start 2024-01-02 \\
      --end 2024-06-30 --out data/aapl.parquet

The output parquet plugs directly into ``run_backtest.py`` and the
``/backtest/run`` API:

  uv run python scripts/run_backtest.py --bars data/equity_2024.parquet \\
      --strategy buy_and_hold
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys
from collections.abc import Sequence
from datetime import date, timedelta

# scripts/ are not packaged; prepend service src dirs so we can import.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
for _src in (
    _REPO_ROOT / "services" / "backtester" / "src",
    _REPO_ROOT / "services" / "oms" / "src",
    _REPO_ROOT / "libs" / "fincept-core" / "src",
):
    if _src.exists() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

import httpx  # noqa: E402

from backtester.ingest import (  # noqa: E402
    assert_parquet_matches_runner_schema,
    bars_to_parquet,
    parse_alpaca_bars_payload,
    parse_yfinance_daily_frame,
)
from fincept_core.config import get_settings  # noqa: E402
from fincept_core.schemas import AssetClass, BarEvent, Venue  # noqa: E402

ALPACA_DATA_BASE = "https://data.alpaca.markets"

# Map our internal freq strings to what each vendor expects.
_ALPACA_TIMEFRAME = {
    "1m": "1Min",
    "5m": "5Min",
    "15m": "15Min",
    "1h": "1Hour",
    "1d": "1Day",
}
_FREQ_FROM_ALPACA = {v: k for k, v in _ALPACA_TIMEFRAME.items()}


# --------------------------------------------------------------------------- #
# Alpaca fetch (with pagination)                                              #
# --------------------------------------------------------------------------- #


async def fetch_alpaca_bars(
    *,
    symbols: list[str],
    timeframe: str,
    start: str,
    end: str,
    api_key: str,
    api_secret: str,
    feed: str = "iex",
    page_limit: int = 10_000,
    max_pages: int = 200,
) -> list[BarEvent]:
    """Walk Alpaca's ``next_page_token`` until done; return parsed bars.

    ``timeframe`` is the Alpaca-native string (e.g. ``"1Min"``).  The
    canonical freq we store on each ``BarEvent`` is derived from it.
    """
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
        "Accept": "application/json",
    }
    freq = _FREQ_FROM_ALPACA.get(timeframe, timeframe.lower())
    all_bars: list[BarEvent] = []
    page_token: str | None = None
    pages = 0
    async with httpx.AsyncClient(timeout=60.0) as http:
        while True:
            params: dict[str, str] = {
                "symbols": ",".join(symbols),
                "timeframe": timeframe,
                "limit": str(page_limit),
                "feed": feed,
                "adjustment": "raw",
                "start": start,
                "end": end,
            }
            if page_token:
                params["page_token"] = page_token
            response = await http.get(
                f"{ALPACA_DATA_BASE}/v2/stocks/bars",
                headers=headers,
                params=params,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Alpaca data error {response.status_code}: {response.text}")
            payload = response.json()
            page_bars = parse_alpaca_bars_payload(
                payload,
                venue=Venue.ALPACA,
                asset_class=AssetClass.EQUITY,
                freq=freq,
            )
            all_bars.extend(page_bars)
            page_token = payload.get("next_page_token")
            pages += 1
            if not page_token or pages >= max_pages:
                break
    return all_bars


# --------------------------------------------------------------------------- #
# yfinance fetch (sync, daily)                                                #
# --------------------------------------------------------------------------- #


def fetch_yfinance_daily(
    *,
    symbols: list[str],
    start: date,
    end: date,
    venue: Venue = Venue.NASDAQ,
) -> list[BarEvent]:
    """Pull daily bars via ``yfinance.download`` and parse to ``BarEvent``\\s.

    yfinance treats ``end`` exclusively, so we pass ``end + 1 day`` to
    include the requested final date.
    """
    import yfinance as yf

    data = yf.download(
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
    if data is None or len(data) == 0:
        return []

    out: list[BarEvent] = []
    for sym in symbols:
        if len(symbols) == 1:
            sym_frame = data
        elif sym in data.columns.get_level_values(0):
            sym_frame = data[sym]
        else:
            continue
        if sym_frame is None or len(sym_frame) == 0:
            continue
        rows = sym_frame.reset_index().to_dict(orient="records")
        out.extend(
            parse_yfinance_daily_frame(sym, rows, venue=venue, asset_class=AssetClass.EQUITY)
        )
    return out


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def _resolve_source(arg_source: str) -> str:
    if arg_source != "auto":
        return arg_source
    key, secret = _alpaca_credentials_or_none()
    return "alpaca" if key and secret else "yfinance"


def _alpaca_credentials_or_none() -> tuple[str | None, str | None]:
    """Resolve Alpaca creds via ``Settings`` (canonical ``FINCEPT_`` prefix),
    falling back to the deprecated unprefixed ``ALPACA_*`` env vars."""
    settings = get_settings()
    key = settings.ALPACA_API_KEY or os.environ.get("ALPACA_API_KEY")
    secret = settings.ALPACA_API_SECRET or os.environ.get("ALPACA_API_SECRET")
    return key, secret


def _alpaca_credentials() -> tuple[str, str]:
    key, secret = _alpaca_credentials_or_none()
    if not key or not secret:
        raise SystemExit(
            "Alpaca credentials not set. Export FINCEPT_ALPACA_API_KEY and "
            "FINCEPT_ALPACA_API_SECRET (or ALPACA_API_KEY / ALPACA_API_SECRET)."
        )
    return key, secret


def _normalize_alpaca_timeframe(value: str) -> str:
    """Accept either internal freq (``1m``) or Alpaca native (``1Min``)."""
    if value in _ALPACA_TIMEFRAME:
        return _ALPACA_TIMEFRAME[value]
    if value in _FREQ_FROM_ALPACA:
        return value
    raise SystemExit(
        f"unknown --timeframe {value!r}; valid: "
        f"{sorted(set(_ALPACA_TIMEFRAME) | set(_FREQ_FROM_ALPACA))}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ingest_bars",
        description="Fetch historical OHLCV into a backtester-ready parquet.",
    )
    parser.add_argument(
        "--source",
        default="auto",
        choices=("auto", "alpaca", "yfinance"),
        help="Data source. 'auto' picks Alpaca if credentials are set.",
    )
    parser.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated tickers (e.g. AAPL,MSFT,SPY).",
    )
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD (inclusive).")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD (inclusive).")
    parser.add_argument(
        "--timeframe",
        default="1d",
        help="Bar size. yfinance: 1d only. Alpaca: 1m|5m|15m|1h|1d "
        "(or native 1Min|5Min|15Min|1Hour|1Day).",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output parquet path (parent dirs created).",
    )
    parser.add_argument(
        "--feed",
        default="iex",
        choices=("iex", "sip"),
        help="Alpaca feed. 'iex' is the free tier; 'sip' requires a paid sub.",
    )
    args = parser.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        raise SystemExit("--symbols must contain at least one ticker")

    try:
        start_d = date.fromisoformat(args.start)
        end_d = date.fromisoformat(args.end)
    except ValueError as exc:
        raise SystemExit(f"bad --start / --end: {exc}") from exc
    if start_d > end_d:
        raise SystemExit(f"--start {start_d} is after --end {end_d}")

    source = _resolve_source(args.source)

    if source == "yfinance":
        if args.timeframe not in ("1d", "1Day"):
            raise SystemExit(
                "yfinance source supports --timeframe 1d only (use --source alpaca for intraday)."
            )
        print(f"[ingest] yfinance daily {symbols} {start_d.isoformat()}..{end_d.isoformat()}")
        bars = fetch_yfinance_daily(symbols=symbols, start=start_d, end=end_d)
    else:
        api_key, api_secret = _alpaca_credentials()
        timeframe = _normalize_alpaca_timeframe(args.timeframe)
        # Alpaca expects RFC-3339; date-only strings are interpreted as
        # 00:00 UTC which is fine for an inclusive day range.  We bump
        # `end` by one day so the close of the requested final date is
        # included regardless of timezone.
        end_iso = (end_d + timedelta(days=1)).isoformat()
        print(
            f"[ingest] alpaca {timeframe} {symbols} "
            f"{start_d.isoformat()}..{end_d.isoformat()} feed={args.feed}"
        )
        bars = asyncio.run(
            fetch_alpaca_bars(
                symbols=symbols,
                timeframe=timeframe,
                start=start_d.isoformat(),
                end=end_iso,
                api_key=api_key,
                api_secret=api_secret,
                feed=args.feed,
            )
        )

    if not bars:
        raise SystemExit(f"no bars returned for {symbols} {start_d}..{end_d} via {source}")

    n = bars_to_parquet(bars, args.out)
    assert_parquet_matches_runner_schema(args.out)

    by_sym: dict[str, int] = {}
    for b in bars:
        by_sym[b.symbol] = by_sym.get(b.symbol, 0) + 1
    print(f"[ingest] wrote {n} rows -> {args.out}")
    print(f"[ingest] per-symbol: {by_sym}")
    print(f"[ingest] ts span: {min(b.ts_event for b in bars)} .. {max(b.ts_event for b in bars)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
