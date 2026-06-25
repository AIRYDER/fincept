"""
quant_foundry.market_data_adapter — price feed for settlement (Agent A).

Provides a sync adapter that fetches historical close prices for the
settlement sweep worker. The adapter tries ``fincept_db.bars`` first and
falls back to an empty list when the database is unavailable or has no
data for the requested window. Missing data causes the settlement ledger
to produce ``PENDING_DATA`` records (distinct from ``PENDING_TIME``).

The adapter returns ``PricePoint`` objects (``ts_ns`` + ``close``). The
settlement sweep worker converts these to ``metrics.PriceTick`` when
calling ``SettlementLedger.settle()``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PricePoint:
    """A close price observed at a known wall-clock time (ns since epoch)."""

    ts_ns: int
    close: float


class BarDataAdapter:
    """Sync adapter that reads close prices from ``fincept_db.bars``.

    Tries the database first; falls back to an empty list when the
    database is unavailable or has no bars for the requested window.
    A callable ``bar_reader`` can be injected for testing or for wiring
    a non-DB source (e.g. Alpaca).
    """

    def __init__(
        self,
        *,
        bar_reader: Callable[[str, int, int], Sequence[PricePoint]] | None = None,
        benchmark_symbol: str = "SPY",
        freq: str = "1min",
    ) -> None:
        self._bar_reader = bar_reader
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
            return [
                PricePoint(ts_ns=int(b.ts_event), close=float(b.close))
                for b in bars
            ]
        except Exception:
            return []
