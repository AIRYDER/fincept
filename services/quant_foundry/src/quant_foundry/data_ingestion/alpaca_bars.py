"""
quant_foundry.data_ingestion.alpaca_bars — fetch OHLCV bars from Alpaca's
market data API and ingest them through the equity pipeline.

This module adds a vendor API adapter for Alpaca (https://alpaca.markets)
that fetches historical OHLCV bars via the market-data REST API and feeds
them into the same leakage-safe :func:`ingest_equity_bars` pipeline used by
local-file ingestion.

We hit Alpaca directly with ``httpx`` (no SDK), matching the pattern in
``services/oms/src/oms/alpaca/client.py``.  The market-data base URL is
``https://data.alpaca.markets`` (NOT the trading API URL).

Env vars (from ``.env.example``):

- ``FINCEPT_ALPACA_API_KEY``    — Alpaca API key
- ``FINCEPT_ALPACA_API_SECRET`` — Alpaca API secret
- ``FINCEPT_ALPACA_BASE_URL``   — trading API base URL (default
  ``https://paper-api.alpaca.markets``); the market-data URL is fixed.

Heavy dependencies (httpx, polars) are imported lazily inside functions so
this module is importable without them.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
from datetime import UTC, datetime
from typing import Any

from quant_foundry.data_ingestion.equities import IngestionResult, ingest_equity_bars

#: Alpaca market-data API base URL.  This is distinct from the trading API
#: URL (``https://paper-api.alpaca.markets``); bars live on the data domain.
ALPACA_DATA_BASE_URL = "https://data.alpaca.markets"

#: Default trading-API base URL, read from ``FINCEPT_ALPACA_BASE_URL`` when
#: no explicit ``base_url`` is supplied.  Kept for completeness; the bars
#: endpoint always uses :data:`ALPACA_DATA_BASE_URL`.
_DEFAULT_TRADING_BASE_URL = "https://paper-api.alpaca.markets"

#: Env var names for Alpaca credentials.
_ALPACA_KEY_ENV = "FINCEPT_ALPACA_API_KEY"
_ALPACA_SECRET_ENV = "FINCEPT_ALPACA_API_SECRET"
_ALPACA_BASE_ENV = "FINCEPT_ALPACA_BASE_URL"


def _resolve_credentials(
    api_key: str | None,
    api_secret: str | None,
    base_url: str | None,
) -> tuple[str, str, str]:
    """Resolve Alpaca credentials, falling back to env vars.

    Returns ``(api_key, api_secret, base_url)``.  Raises ``ValueError`` with
    a clear message if either credential is missing.
    """
    key = api_key or os.environ.get(_ALPACA_KEY_ENV, "")
    secret = api_secret or os.environ.get(_ALPACA_SECRET_ENV, "")
    url = base_url or os.environ.get(_ALPACA_BASE_ENV, _DEFAULT_TRADING_BASE_URL)
    if not key:
        raise ValueError(
            f"Alpaca API key not provided; pass api_key= or set "
            f"{_ALPACA_KEY_ENV}",
        )
    if not secret:
        raise ValueError(
            f"Alpaca API secret not provided; pass api_secret= or set "
            f"{_ALPACA_SECRET_ENV}",
        )
    return key, secret, url


def _iso_to_ns(ts: str) -> int:
    """Parse an Alpaca ISO-8601 timestamp to UTC nanoseconds since epoch.

    Alpaca returns timestamps like ``"2024-01-02T05:00:00Z"``.  Naive
    timestamps are assumed UTC (Alpaca always returns Z/UTC).
    """
    normalized = ts.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return int(parsed.timestamp() * 1_000_000_000)


def _iso_to_date_str(ts: str) -> str:
    """Parse an Alpaca ISO-8601 timestamp to a ``YYYY-MM-DD`` date string."""
    normalized = ts.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.date().isoformat()


async def fetch_alpaca_bars(
    *,
    symbols: list[str],
    start: str,
    end: str,
    timeframe: str = "1Day",
    api_key: str | None = None,
    api_secret: str | None = None,
    base_url: str | None = None,
) -> pathlib.Path:
    """Fetch OHLCV bars from Alpaca and write to a temp parquet file.

    Parameters
    ----------
    symbols
        List of ticker symbols to fetch (e.g. ``["AAPL", "MSFT"]``).
    start
        ISO date string for the range start (e.g. ``"2024-01-01"``).
    end
        ISO date string for the range end (e.g. ``"2024-06-30"``).
    timeframe
        Bar timeframe as accepted by Alpaca (default ``"1Day"``).
    api_key
        Alpaca API key.  Falls back to ``FINCEPT_ALPACA_API_KEY``.
    api_secret
        Alpaca API secret.  Falls back to ``FINCEPT_ALPACA_API_SECRET``.
    base_url
        Trading API base URL (unused for bars; kept for API symmetry).
        Falls back to ``FINCEPT_ALPACA_BASE_URL``.

    Returns
    -------
    pathlib.Path
        Path to the temp parquet file containing the fetched bars with
        columns ``symbol, date, ts_event, open, high, low, close, volume``.

    Raises
    ------
    ValueError
        If credentials are missing or no bars are returned for any symbol.
    """
    import httpx
    import polars as pl

    key, secret, _ = _resolve_credentials(api_key, api_secret, base_url)
    headers = {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }

    all_frames: list[pl.DataFrame] = []
    async with httpx.AsyncClient(
        base_url=ALPACA_DATA_BASE_URL,
        headers=headers,
        timeout=30.0,
    ) as client:
        for symbol in symbols:
            params = {
                "start": start,
                "end": end,
                "timeframe": timeframe,
                "limit": "10000",
            }
            resp = await client.get(
                f"/v2/stocks/{symbol}/bars",
                params=params,
            )
            resp.raise_for_status()
            body = resp.json()
            raw_bars = body.get("bars") or []
            if not raw_bars:
                continue
            rows: dict[str, list[Any]] = {
                "symbol": [],
                "date": [],
                "ts_event": [],
                "open": [],
                "high": [],
                "low": [],
                "close": [],
                "volume": [],
            }
            for bar in raw_bars:
                # Alpaca bars fields: t (timestamp), o, h, l, c, v.
                rows["symbol"].append(symbol)
                rows["date"].append(_iso_to_date_str(bar["t"]))
                rows["ts_event"].append(_iso_to_ns(bar["t"]))
                rows["open"].append(float(bar["o"]))
                rows["high"].append(float(bar["h"]))
                rows["low"].append(float(bar["l"]))
                rows["close"].append(float(bar["c"]))
                rows["volume"].append(float(bar["v"]))
            all_frames.append(pl.DataFrame(rows))

    if not all_frames:
        raise ValueError(
            f"no bars returned from Alpaca for symbols {symbols} "
            f"in [{start}, {end}]",
        )

    combined = pl.concat(all_frames, how="vertical_relaxed")
    tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="alpaca_bars_"))
    out_path = tmp_dir / "alpaca_bars.parquet"
    combined.write_parquet(str(out_path))
    return out_path


async def ingest_alpaca_equity_bars(
    *,
    symbols: list[str],
    start: str,
    end: str,
    output_dir: pathlib.Path,
    dataset_id: str,
    timeframe: str = "1Day",
    label_horizon_days: int = 5,
    n_folds: int = 3,
    api_key: str | None = None,
    api_secret: str | None = None,
) -> IngestionResult:
    """Fetch bars from Alpaca and ingest into a leakage-safe dataset.

    Fetches OHLCV bars via :func:`fetch_alpaca_bars`, then runs the full
    :func:`ingest_equity_bars` pipeline (features + labels + manifest +
    receipt + quality report).

    Parameters
    ----------
    symbols
        List of ticker symbols to fetch.
    start
        ISO date string for the range start.
    end
        ISO date string for the range end.
    output_dir
        Directory to write the dataset artifacts.  Created if needed.
    dataset_id
        Unique dataset identifier.
    timeframe
        Bar timeframe (default ``"1Day"``).
    label_horizon_days
        Forward-return label horizon in days (default 5).
    n_folds
        Number of purged-k-fold validation windows (default 3).
    api_key
        Alpaca API key; falls back to env var.
    api_secret
        Alpaca API secret; falls back to env var.

    Returns
    -------
    IngestionResult
        Paths to all emitted artifacts plus the manifest and quality report.
    """
    bars_path = await fetch_alpaca_bars(
        symbols=symbols,
        start=start,
        end=end,
        timeframe=timeframe,
        api_key=api_key,
        api_secret=api_secret,
    )
    return ingest_equity_bars(
        bars_path,
        output_dir=pathlib.Path(output_dir),
        dataset_id=dataset_id,
        symbols=symbols,
        label_horizon_days=label_horizon_days,
        n_folds=n_folds,
        source_vintage_refs=[
            "vendor:alpaca",
            f"timeframe:{timeframe}",
            f"range:{start}..{end}",
        ],
    )


__all__ = [
    "ALPACA_DATA_BASE_URL",
    "fetch_alpaca_bars",
    "ingest_alpaca_equity_bars",
]
