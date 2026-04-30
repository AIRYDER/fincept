"""
backtester.runner - high-level "parquet -> engine -> report" wrapper.

Used by both the CLI (``scripts/run_backtest.py``) and the API
(``/backtest/run``) so a backtest is invoked the same way from both
surfaces.  The runner:

  1. Reads bars from a Parquet file (the canonical local format the
     ingestor's :mod:`scripts.capture_to_parquet` writes).
  2. Builds an in-memory ``BarReader`` so :class:`BarsDataSource` doesn't
     hit Timescale (the backtester is meant to be runnable offline).
  3. Instantiates a strategy from the registry.
  4. Runs the engine, computes the report, returns the typed payload.
  5. Optionally writes the report JSON + a small ``run.json`` manifest
     under ``reports/backtests/<run_id>/``.

The Parquet schema mirrors :class:`fincept_core.schemas.BarEvent`:
required columns are ``symbol, ts_event, open, high, low, close, volume``;
optional columns are ``high, low, vwap, trades``.  Decimal fields are
read as strings to preserve precision.

A run record on disk has this layout::

    reports/backtests/<run_id>/
      run.json       summary: id, params, started_at, finished_at, status
      report.json    full BacktestReport (equity curve + trades inline)

This is the contract the API's /backtest/runs endpoint walks.
"""

from __future__ import annotations

import json
import pathlib
import time
import uuid
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

import polars as pl

from backtester.blotter import Blotter
from backtester.broker import SimBroker
from backtester.costs import CostModel
from backtester.datasource import BarsDataSource
from backtester.engine import BacktestEngine
from backtester.report import BacktestReport, compute_metrics, report_to_dict
from backtester.strategies import STRATEGY_REGISTRY
from fincept_core.config import Settings
from fincept_core.schemas import AssetClass, BarEvent, Venue
from fincept_sdk import Strategy

REPORTS_ROOT = pathlib.Path("reports/backtests")


# --------------------------------------------------------------------------- #
# Parquet -> BarEvent loader                                                  #
# --------------------------------------------------------------------------- #


_REQUIRED_COLS = ("symbol", "ts_event", "open", "high", "low", "close", "volume")


def _coerce_decimal(value: Any) -> Decimal:
    """Robust scalar -> Decimal that handles polars' returned types."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal(0)
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    return Decimal(str(value))


def _row_to_bar(
    row: dict[str, Any],
    *,
    venue: Venue,
    asset_class: AssetClass,
    freq: str,
) -> BarEvent:
    return BarEvent(
        venue=venue,
        symbol=str(row["symbol"]),
        asset_class=asset_class,
        ts_event=int(row["ts_event"]),
        ts_recv=int(row["ts_event"]),
        freq=freq,
        open=_coerce_decimal(row["open"]),
        high=_coerce_decimal(row["high"]),
        low=_coerce_decimal(row["low"]),
        close=_coerce_decimal(row["close"]),
        volume=_coerce_decimal(row["volume"]),
        trades=int(row.get("trades") or 0),
        vwap=_coerce_decimal(row["vwap"]) if row.get("vwap") is not None else None,
    )


def load_bars_from_parquet(
    path: pathlib.Path | str,
    *,
    venue: Venue = Venue.PAPER,
    asset_class: AssetClass = AssetClass.CRYPTO_SPOT,
    freq: str = "1m",
) -> dict[str, list[BarEvent]]:
    """Read ``path``, return ``{symbol: [BarEvent, ...]}`` sorted by ts_event.

    Validates required columns up-front so the engine doesn't fail
    halfway through a run with a cryptic AttributeError.
    """
    df = pl.read_parquet(path)
    missing = [c for c in _REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(
            f"parquet at {path} is missing required columns: {missing}"
        )
    df = df.sort(["symbol", "ts_event"])
    by_symbol: dict[str, list[BarEvent]] = {}
    for row in df.to_dicts():
        bar = _row_to_bar(row, venue=venue, asset_class=asset_class, freq=freq)
        by_symbol.setdefault(bar.symbol, []).append(bar)
    return by_symbol


def make_bar_reader(
    bars_by_symbol: dict[str, list[BarEvent]],
) -> Any:
    """Closure that the BarsDataSource calls per symbol/range."""

    async def reader(
        symbol: str, freq: str, start_ns: int, end_ns: int, **_: Any
    ) -> list[BarEvent]:
        bars = bars_by_symbol.get(symbol, [])
        return [b for b in bars if start_ns <= b.ts_event < end_ns]

    return reader


# --------------------------------------------------------------------------- #
# Strategy factory                                                            #
# --------------------------------------------------------------------------- #


def build_strategy(
    name: str,
    *,
    symbols: Iterable[str],
    params: dict[str, Any] | None = None,
) -> Strategy:
    """Look up the strategy class in the registry and instantiate it.

    ``params`` is a free-form dict of constructor kwargs; unknown keys
    raise ``TypeError`` from the strategy class which surfaces a clear
    error to the API caller.  ``per_symbol_notional`` is auto-converted
    from float/int/string to ``Decimal`` since JSON only carries numerics.
    """
    if name not in STRATEGY_REGISTRY:
        raise ValueError(
            f"unknown strategy {name!r}; valid: {sorted(STRATEGY_REGISTRY)}"
        )
    cls = STRATEGY_REGISTRY[name]
    kwargs: dict[str, Any] = dict(params or {})
    if "per_symbol_notional" in kwargs:
        kwargs["per_symbol_notional"] = _coerce_decimal(
            kwargs["per_symbol_notional"]
        )
    instance: Strategy = cls(symbols=list(symbols), **kwargs)
    return instance


# --------------------------------------------------------------------------- #
# End-to-end runner                                                           #
# --------------------------------------------------------------------------- #


class RunResult:
    """Returned from :func:`run_backtest` so the caller doesn't need to
    re-derive any of the inputs.

    Attributes:
      run_id            stable UUID4 string for this run
      report            the typed :class:`BacktestReport`
      blotter           full ``Blotter`` (fills + equity curve)
      run_dir           on-disk directory if persisted, else ``None``
      manifest          summary dict (params, timing, status) as written
                        to ``run.json``
    """

    def __init__(
        self,
        *,
        run_id: str,
        report: BacktestReport,
        blotter: Blotter,
        run_dir: pathlib.Path | None,
        manifest: dict[str, Any],
    ) -> None:
        self.run_id = run_id
        self.report = report
        self.blotter = blotter
        self.run_dir = run_dir
        self.manifest = manifest


async def run_backtest(
    *,
    parquet_path: pathlib.Path | str,
    strategy_name: str,
    strategy_params: dict[str, Any] | None = None,
    starting_cash: Decimal = Decimal("100000"),
    cost_model: CostModel | None = None,
    risk_settings: Settings | None = None,
    venue: Venue = Venue.PAPER,
    asset_class: AssetClass = AssetClass.CRYPTO_SPOT,
    freq: str = "1m",
    bars_per_year: int | None = None,
    persist: bool = True,
    reports_root: pathlib.Path | None = None,
    run_id: str | None = None,
) -> RunResult:
    """Run a backtest end-to-end and (optionally) persist the report.

    ``bars_per_year`` defaults to a value derived from ``freq``:
      - ``1m`` -> 525,600 bars/year (default)
      - ``1h`` -> 8,760
      - ``1d`` -> 252 (trading days; reasonable for daily bars)

    Set ``persist=False`` for tests / stateless API previews where a
    report should be returned but not written to disk.
    """
    started_at = int(time.time())
    run_id = run_id or uuid.uuid4().hex
    bars_by_symbol = load_bars_from_parquet(
        parquet_path,
        venue=venue,
        asset_class=asset_class,
        freq=freq,
    )
    if not bars_by_symbol:
        raise ValueError(f"parquet at {parquet_path} contains no rows")

    symbols = sorted(bars_by_symbol)
    # Use the actual span of the data; +1 ns on end so the inclusive
    # filter in the reader returns the final bar.
    flat_bars = [b for bars in bars_by_symbol.values() for b in bars]
    start_ns = min(b.ts_event for b in flat_bars)
    end_ns = max(b.ts_event for b in flat_bars) + 1

    bar_reader = make_bar_reader(bars_by_symbol)
    datasource = BarsDataSource(
        symbols=symbols,
        freq=freq,
        start_ns=start_ns,
        end_ns=end_ns,
        bar_reader=bar_reader,
    )
    strategy = build_strategy(
        strategy_name, symbols=symbols, params=strategy_params
    )
    broker = SimBroker(cost_model=cost_model or CostModel())
    blotter = Blotter(starting_cash=starting_cash)
    engine = BacktestEngine(
        strategy=strategy,
        datasource=datasource,
        broker=broker,
        blotter=blotter,
        risk_settings=risk_settings,
    )
    await engine.run()

    if bars_per_year is None:
        bars_per_year = _bars_per_year_for_freq(freq)
    report = compute_metrics(blotter, bars_per_year=bars_per_year)

    manifest = {
        "run_id": run_id,
        "status": "complete",
        "started_at": started_at,
        "finished_at": int(time.time()),
        "parquet_path": str(parquet_path),
        "strategy_name": strategy_name,
        "strategy_params": strategy_params or {},
        "starting_cash": float(starting_cash),
        "freq": freq,
        "venue": str(venue),
        "asset_class": str(asset_class),
        "bars_per_year": bars_per_year,
        "symbols": symbols,
        "start_ns": start_ns,
        "end_ns": end_ns,
        "n_bars": report.n_bars,
        "n_fills": report.n_fills,
        "final_equity": report.final_equity,
        "total_return_pct": report.total_return_pct,
        "sharpe": report.sharpe,
        "max_drawdown_pct": report.max_drawdown_pct,
    }

    run_dir: pathlib.Path | None = None
    if persist:
        root = reports_root or REPORTS_ROOT
        run_dir = root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(json.dumps(manifest, indent=2))
        (run_dir / "report.json").write_text(
            json.dumps(report_to_dict(report), indent=2)
        )

    return RunResult(
        run_id=run_id,
        report=report,
        blotter=blotter,
        run_dir=run_dir,
        manifest=manifest,
    )


def _bars_per_year_for_freq(freq: str) -> int:
    """Map common bar frequencies to annualization factors."""
    return {
        "1m": 365 * 24 * 60,
        "5m": 365 * 24 * 12,
        "15m": 365 * 24 * 4,
        "1h": 365 * 24,
        "1d": 252,
    }.get(freq, 365 * 24 * 60)


# --------------------------------------------------------------------------- #
# Run discovery (for the API's list endpoint)                                 #
# --------------------------------------------------------------------------- #


def list_run_manifests(
    reports_root: pathlib.Path | None = None,
) -> list[dict[str, Any]]:
    """Return every persisted run's ``run.json`` manifest, newest first.

    Used by ``GET /backtest/runs``.  Malformed manifests are skipped
    silently so one corrupt run doesn't break the listing.
    """
    root = reports_root or REPORTS_ROOT
    if not root.exists():
        return []
    manifests: list[dict[str, Any]] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        manifest_path = entry / "run.json"
        if not manifest_path.exists():
            continue
        try:
            manifests.append(json.loads(manifest_path.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    manifests.sort(
        key=lambda m: int(m.get("started_at") or 0), reverse=True
    )
    return manifests


def load_run_report(
    run_id: str, *, reports_root: pathlib.Path | None = None
) -> dict[str, Any] | None:
    """Read ``reports/backtests/<run_id>/report.json`` if present."""
    root = reports_root or REPORTS_ROOT
    report_path = root / run_id / "report.json"
    if not report_path.exists():
        return None
    try:
        loaded = json.loads(report_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return loaded if isinstance(loaded, dict) else None
