"""
quant_foundry.market_data_adapter — price feed for settlement (Agent A).

Provides a sync adapter that fetches historical close prices for the
settlement sweep worker. The adapter tries ``fincept_db.bars`` first,
then an optional Alpaca fallback, and falls back to an empty list when
neither source has data for the requested window. Missing data causes
the settlement ledger to produce ``PENDING_DATA`` records (distinct from
``PENDING_TIME``).

The adapter returns ``PricePoint`` objects (``ts_ns`` + ``close``). The
settlement sweep worker converts these to ``metrics.PriceTick`` when
calling ``SettlementLedger.settle()``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .gateway_helpers import env_first


@dataclass(frozen=True)
class PricePoint:
    """A close price observed at a known wall-clock time (ns since epoch)."""

    ts_ns: int
    close: float


class BarDataAdapter:
    """Sync adapter that reads close prices from ``fincept_db.bars``
    with an optional Alpaca fallback.

    Tries the database first; if no bars are found and an
    ``alpaca_reader`` is configured, tries Alpaca. Falls back to an
    empty list when neither source has data. A callable ``bar_reader``
    can be injected for testing or for wiring a non-DB source.
    """

    def __init__(
        self,
        *,
        bar_reader: Callable[[str, int, int], Sequence[PricePoint]] | None = None,
        alpaca_reader: Callable[[str, int, int], Sequence[PricePoint]] | None = None,
        benchmark_symbol: str = "SPY",
        freq: str = "1min",
    ) -> None:
        self._bar_reader = bar_reader
        self._alpaca_reader = alpaca_reader
        self._benchmark_symbol = benchmark_symbol
        self._freq = freq

    def get_prices(
        self,
        symbol: str,
        start_ns: int,
        end_ns: int,
    ) -> list[PricePoint]:
        """Return sorted price points for *symbol* in ``[start_ns, end_ns)``.

        Returns an empty list when no data is available (settlement will
        produce ``PENDING_DATA``).
        """
        if self._bar_reader is not None:
            points = list(self._bar_reader(symbol, start_ns, end_ns))
        else:
            points = self._try_fincept_db(symbol, start_ns, end_ns)
        if not points and self._alpaca_reader is not None:
            points = list(self._alpaca_reader(symbol, start_ns, end_ns))
        points.sort(key=lambda p: p.ts_ns)
        return points

    def get_benchmark_prices(
        self,
        start_ns: int,
        end_ns: int,
    ) -> list[PricePoint]:
        """Return sorted benchmark (default SPY) prices in ``[start_ns, end_ns)``.

        Returns an empty list when no data is available.
        """
        return self.get_prices(self._benchmark_symbol, start_ns, end_ns)

    def _try_fincept_db(
        self,
        symbol: str,
        start_ns: int,
        end_ns: int,
    ) -> list[PricePoint]:
        """Attempt to read bars from ``fincept_db.bars`` (async API).

        Returns an empty list on any failure (import error, DB error,
        no event loop, etc.). Never raises — a stuck provider must not
        crash the settlement sweep.
        """
        try:
            import asyncio

            from fincept_db.bars import read_bars

            bars = asyncio.run(
                read_bars(
                    symbol=symbol,
                    freq=self._freq,
                    start_ns=start_ns,
                    end_ns=end_ns,
                )
            )
            return [PricePoint(ts_ns=int(b.ts_event), close=float(b.close)) for b in bars]
        except Exception:
            return []


def alpaca_reader_from_env() -> Callable[[str, int, int], list[PricePoint]] | None:
    """Build an Alpaca bar reader from env vars, or return None.

    Reads ``FINCEPT_ALPACA_API_KEY`` and ``FINCEPT_ALPACA_API_SECRET``
    (canonical, via the ``Settings`` env prefix); falls back to the
    deprecated unprefixed ``ALPACA_API_KEY`` / ``ALPACA_API_SECRET``
    names with a ``DeprecationWarning``. When both key and secret are
    present, returns a sync callable that fetches 1-min bars from
    Alpaca's data API and converts them to ``PricePoint`` objects.
    When either is missing, returns ``None`` (settlement falls back
    to ``fincept_db.bars`` only).
    """
    api_key = env_first("FINCEPT_ALPACA_API_KEY", "ALPACA_API_KEY")
    api_secret = env_first("FINCEPT_ALPACA_API_SECRET", "ALPACA_API_SECRET")
    if not api_key or not api_secret:
        return None

    def reader(symbol: str, start_ns: int, end_ns: int) -> list[PricePoint]:
        return _fetch_alpaca_bars_sync(
            symbol=symbol,
            start_ns=start_ns,
            end_ns=end_ns,
            api_key=api_key,
            api_secret=api_secret,
        )

    return reader


def _fetch_alpaca_bars_sync(
    *,
    symbol: str,
    start_ns: int,
    end_ns: int,
    api_key: str,
    api_secret: str,
) -> list[PricePoint]:
    """Fetch 1-min bars from Alpaca synchronously via httpx.

    Never raises — on any failure returns an empty list so settlement
    produces ``PENDING_DATA`` instead of crashing.
    """
    try:
        import asyncio
        from datetime import UTC, datetime

        import httpx

        start_iso = datetime.fromtimestamp(start_ns / 1_000_000_000, tz=UTC)
        end_iso = datetime.fromtimestamp(end_ns / 1_000_000_000, tz=UTC)
        start_str = start_iso.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = end_iso.strftime("%Y-%m-%dT%H:%M:%SZ")

        async def _fetch() -> list[PricePoint]:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://data.alpaca.markets/v2/stocks/bars",
                    headers={
                        "APCA-API-KEY-ID": api_key,
                        "APCA-API-SECRET-KEY": api_secret,
                        "Accept": "application/json",
                    },
                    params={
                        "symbols": symbol,
                        "timeframe": "1Min",
                        "start": start_str,
                        "end": end_str,
                        "limit": "1000",
                        "feed": "iex",
                        "adjustment": "raw",
                    },
                )
                if resp.status_code >= 400:
                    return []
                data = resp.json()
                raw_bars = data.get("bars", {}).get(symbol) or []
                return [
                    PricePoint(
                        ts_ns=_iso_to_ns(bar["t"]),
                        close=float(bar["c"]),
                    )
                    for bar in raw_bars
                    if "t" in bar and "c" in bar
                ]

        return asyncio.run(_fetch())
    except Exception:
        return []


def _iso_to_ns(iso_str: str) -> int:
    """Convert an ISO-8601 timestamp string to nanoseconds since epoch."""
    from datetime import UTC, datetime

    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1_000_000_000)
