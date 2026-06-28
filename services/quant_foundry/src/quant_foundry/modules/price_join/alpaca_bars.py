"""
quant_foundry.modules.price_join.alpaca_bars — price joiner via Alpaca bars.

Loads OHLCV bars for the universe from a parquet file (or directory of
per-symbol parquets) and benchmark bars (SPY by default).  Converts
them to :class:`PriceBar` objects for the label computer.

This module wraps the existing
``scripts.build_dataset_manifest.load_bars_from_parquet`` loader so it
reuses the same parquet schema (``symbol, ts_event, open, high, low,
close, volume``).

This module is registered as ``price_join:alpaca-bars:1.0.0``.
"""

from __future__ import annotations

import pathlib
from typing import Any

from quant_foundry.modules.registry import (
    ModuleInfo,
    PriceBar,
    register_module,
)

NS_PER_DAY = 86_400_000_000_000


@register_module(
    "price_join",
    "alpaca-bars",
    "1.0.0",
    default_config={
        "bars_dir": "data/bars/",
        "benchmark_symbol": "SPY",
    },
)
class AlpacaBarsPriceJoin:
    """Load price bars from parquet files.

    Expects parquet files produced by ``scripts/ingest_bars.py`` or the
    Alpaca adapter (``data_ingestion/alpaca_bars.py``).  Files may be a
    single multi-symbol parquet or one parquet per symbol named
    ``<symbol>.parquet``.

    The benchmark (default SPY) is loaded from the same directory.
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.bars_dir = pathlib.Path(self.config.get("bars_dir", "data/bars/"))
        self.benchmark_symbol = self.config.get("benchmark_symbol", "SPY")

    def load_bars(
        self,
        *,
        symbols: list[str],
        start_ns: int,
        end_ns: int,
    ) -> tuple[dict[str, list[PriceBar]], list[PriceBar]]:
        """Load asset + benchmark bars.

        Returns ``(asset_bars, benchmark_bars)`` where ``asset_bars`` is
        ``{symbol: [PriceBar, ...]}`` and ``benchmark_bars`` is a flat
        list for the benchmark symbol.
        """
        import polars as pl

        all_symbols = list(symbols)
        if self.benchmark_symbol not in all_symbols:
            all_symbols.append(self.benchmark_symbol)

        # Collect candidate parquet files.
        candidates: list[pathlib.Path] = []
        if self.bars_dir.is_dir():
            for sym in all_symbols:
                p = self.bars_dir / f"{sym}.parquet"
                if p.exists():
                    candidates.append(p)
            for p in sorted(self.bars_dir.glob("*.parquet")):
                if p not in candidates:
                    candidates.append(p)

        if not candidates:
            return {}, []

        frames: list[pl.DataFrame] = []
        for path in candidates:
            df = pl.read_parquet(str(path))
            keep = [
                c for c in ("symbol", "ts_event", "open", "high", "low", "close", "volume")
                if c in df.columns
            ]
            df = df.select(keep)
            frames.append(df)

        combined = pl.concat(frames, how="vertical_relaxed")
        combined = combined.filter(
            (pl.col("ts_event") >= start_ns) & (pl.col("ts_event") < end_ns),
        )
        combined = combined.filter(pl.col("symbol").is_in(all_symbols))

        asset_bars: dict[str, list[PriceBar]] = {}
        benchmark_bars: list[PriceBar] = []

        for sym in all_symbols:
            sub = combined.filter(pl.col("symbol") == sym).sort("ts_event")
            if sub.height == 0:
                continue
            bars = [
                PriceBar(
                    symbol=sym,
                    ts_ns=int(row["ts_event"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
                for row in sub.iter_rows(named=True)
            ]
            if sym == self.benchmark_symbol:
                benchmark_bars = bars
            else:
                asset_bars[sym] = bars

        return asset_bars, benchmark_bars


__all__ = ["AlpacaBarsPriceJoin"]
