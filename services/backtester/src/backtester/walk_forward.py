"""
backtester.walk_forward — expanding-window walk-forward evaluation.

Each fold trains a fresh GBM on bars ``[t0, train_end_k]``, leaves a
purge gap of ``purge_bars``, and backtests on the next ``val_bars``
out-of-sample.  Per-fold reports are aggregated into a single OOS
equity curve (returns concatenated; positions are *not* carried across
folds — see "Why returns instead of equity?" below).

Why returns instead of carrying positions/equity?
  Carrying state across folds entangles strategy state (e.g.,
  ``_is_long``) with research methodology and lets a stale signal from
  fold k dictate trades in fold k+1 with a *different* model.
  Concatenating returns is the standard academic walk-forward
  treatment: each fold is independent ("train on history, test on next
  slice"), and the OOS curve is the geometric compounding of per-bar
  returns within each fold.

Fold layout (expanding window)::

    fold 0:  train[0..T0]                purge val[T0+P..T0+P+V]
    fold 1:  train[0..T1]                purge val[T1+P..T1+P+V]
    fold 2:  train[0..T2]                purge val[T2+P..T2+P+V]
    ...
    where T_{k+1} = T_k + V + embargo

Public surface:
  - :func:`make_folds`            pure index math; tested in isolation
  - :func:`build_training_matrix` bars + features + horizon -> (X, y)
  - :class:`WalkForwardReport`    pydantic, JSON-serialisable
  - :func:`walk_forward_backtest` the top-level coroutine
"""

from __future__ import annotations

import json
import math
import pathlib
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from backtester.blotter import Blotter
from backtester.broker import SimBroker
from backtester.costs import CostModel
from backtester.datasource import BarsDataSource
from backtester.engine import BacktestEngine
from backtester.gbm_features import (
    compute_features,
    require_supported,
    required_window_bars,
)
from backtester.report import compute_metrics
from backtester.runner import (
    _bars_per_year_for_freq,
    load_bars_from_parquet,
    make_bar_reader,
)
from backtester.strategies import GBMStrategy
from fincept_core.config import Settings
from fincept_core.datasets.cv import Fold as _SharedFold
from fincept_core.datasets.cv import make_folds as _shared_make_folds
from fincept_core.schemas import AssetClass, BarEvent, Venue

# --------------------------------------------------------------------------- #
# Fold splitting                                                              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Fold:
    """Half-open index ranges into the canonical timestamp grid.

    All ranges are ``[start, end)``; ``end`` is exclusive so concatenation
    works cleanly with Python slicing.
    """

    index: int
    train_start: int
    train_end: int  # exclusive
    val_start: int
    val_end: int  # exclusive

    @property
    def train_bars(self) -> int:
        return self.train_end - self.train_start

    @property
    def val_bars(self) -> int:
        return self.val_end - self.val_start


def _to_local_fold(shared: _SharedFold) -> Fold:
    """Convert a shared Pydantic ``Fold`` to the local dataclass ``Fold``.

    The canonical :class:`fincept_core.datasets.cv.Fold` is a Pydantic
    model; this module's historical :class:`Fold` is a frozen dataclass
    with the same fields.  We translate so callers that import
    ``backtester.walk_forward.Fold`` keep getting dataclass instances
    (preserving ``isinstance`` / ``dataclasses.fields`` expectations).
    """
    return Fold(
        index=shared.index,
        train_start=shared.train_start,
        train_end=shared.train_end,
        val_start=shared.val_start,
        val_end=shared.val_end,
    )


def _make_folds_local(
    n_bars: int,
    *,
    n_folds: int,
    train_min_bars: int,
    val_bars: int,
    purge_bars: int = 0,
    embargo_bars: int = 0,
) -> list[Fold]:
    """Internal fold builder delegating to the shared CV utility.

    Returns local :class:`Fold` dataclass instances so the rest of this
    module (and external callers that import
    ``backtester.walk_forward.Fold``) keep working unchanged.  This
    private helper does *not* emit a deprecation warning so internal
    call sites (e.g. :func:`walk_forward_backtest`) stay quiet.
    """
    return [
        _to_local_fold(f)
        for f in _shared_make_folds(
            n_bars,
            n_folds=n_folds,
            train_min_bars=train_min_bars,
            val_bars=val_bars,
            purge_bars=purge_bars,
            embargo_bars=embargo_bars,
        )
    ]


def make_folds(
    n_bars: int,
    *,
    n_folds: int,
    train_min_bars: int,
    val_bars: int,
    purge_bars: int = 0,
    embargo_bars: int = 0,
) -> list[Fold]:
    """Deprecated thin shim around :func:`fincept_core.datasets.cv.make_folds`.

    The canonical expanding-window purged+embargoed fold math now lives
    in ``fincept_core.datasets.cv`` (re-exported from
    ``fincept_core.datasets``).  This wrapper remains so existing
    imports of ``make_folds`` from the backtester path keep working,
    but it emits a :class:`DeprecationWarning` directing callers to
    the shared utility.

    Returned folds are local :class:`Fold` dataclass instances (not the
    Pydantic ``fincept_core.datasets.cv.Fold``) to preserve the
    historical return type.  The underlying validation error messages
    are identical to the previous inline implementation because the
    shared utility is a verbatim port of it.

    .. deprecated::
        Use :func:`fincept_core.datasets.cv.make_folds` (or
        ``fincept_core.datasets.make_folds``) instead.
    """
    import warnings

    warnings.warn(
        "backtester.walk_forward.make_folds is deprecated; import "
        "make_folds from fincept_core.datasets.cv (or "
        "fincept_core.datasets) instead. The local Fold dataclass is "
        "retained for backwards compatibility.",
        DeprecationWarning,
        stacklevel=2,
    )
    return _make_folds_local(
        n_bars,
        n_folds=n_folds,
        train_min_bars=train_min_bars,
        val_bars=val_bars,
        purge_bars=purge_bars,
        embargo_bars=embargo_bars,
    )


# --------------------------------------------------------------------------- #
# Training matrix                                                             #
# --------------------------------------------------------------------------- #


def build_training_matrix(
    bars_by_symbol: dict[str, list[BarEvent]],
    *,
    feature_names: Sequence[str],
    horizon_bars: int,
    bar_minutes: int,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Pool feature rows + horizon labels across symbols.

    Label is the sign of the forward return ``log(close[i+H] / close[i])``.
    Rows where any feature is undefined (insufficient warmup, non-positive
    close) or where the forward return is undefined (last H bars) are
    dropped.  Returns ``(X, y, info)`` where ``info`` records per-symbol
    row counts so callers can spot symbols contributing nothing.
    """
    if horizon_bars < 1:
        raise ValueError(f"horizon_bars must be >= 1, got {horizon_bars}")
    require_supported(feature_names)
    window = required_window_bars(feature_names, bar_minutes=bar_minutes)

    rows: list[list[float]] = []
    labels: list[int] = []
    per_symbol: dict[str, int] = {}
    for symbol, bars in bars_by_symbol.items():
        n = len(bars)
        contributed = 0
        for i in range(window - 1, n - horizon_bars):
            sub = bars[i - (window - 1) : i + 1]
            feats = compute_features(
                sub, feature_names=list(feature_names), bar_minutes=bar_minutes
            )
            if feats is None:
                continue
            close_i = float(bars[i].close)
            close_future = float(bars[i + horizon_bars].close)
            if close_i <= 0 or close_future <= 0:
                continue
            rows.append([feats[name] for name in feature_names])
            labels.append(1 if close_future > close_i else 0)
            contributed += 1
        per_symbol[symbol] = contributed

    if not rows:
        raise ValueError(
            "no usable training rows produced — every (symbol, bar) was "
            "dropped due to insufficient warmup or non-positive close. "
            f"Per-symbol row counts: {per_symbol}"
        )
    x = np.asarray(rows, dtype=np.float64)
    y = np.asarray(labels, dtype=np.int32)
    return x, y, {"per_symbol_rows": per_symbol, "window_bars": window}


# --------------------------------------------------------------------------- #
# Per-fold training                                                           #
# --------------------------------------------------------------------------- #


def _train_fold_model(
    *,
    train_bars_by_symbol: dict[str, list[BarEvent]],
    feature_names: list[str],
    horizon_bars: int,
    bar_minutes: int,
    out_dir: pathlib.Path,
    num_boost_round: int = 100,
    learning_rate: float = 0.05,
    num_leaves: int = 7,
) -> dict[str, Any]:
    """Train a LightGBM Booster on the fold's training slice and persist
    ``model.txt`` + ``meta.json`` to ``out_dir``.  Returns the meta dict
    so the caller can record it in the fold report.

    Lazy ``lightgbm`` import keeps the module importable in environments
    where the heavy dep isn't installed (e.g., pure unit-test runs of
    :func:`make_folds`).
    """
    import lightgbm as lgb

    x, y, info = build_training_matrix(
        train_bars_by_symbol,
        feature_names=feature_names,
        horizon_bars=horizon_bars,
        bar_minutes=bar_minutes,
    )
    booster = lgb.train(
        params={
            "objective": "binary",
            "metric": "binary_logloss",
            "verbosity": -1,
            "num_leaves": num_leaves,
            "learning_rate": learning_rate,
        },
        train_set=lgb.Dataset(x, label=y, feature_name=feature_names),
        num_boost_round=num_boost_round,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(out_dir / "model.txt"))
    horizon_ns = int(horizon_bars * bar_minutes * 60 * 1_000_000_000)
    meta = {
        "features": feature_names,
        "horizon_bars": horizon_bars,
        "horizon_ns": horizon_ns,
        "train_rows": int(x.shape[0]),
        "train_pos_rate": float(y.mean()),
        "num_boost_round": num_boost_round,
        "per_symbol_rows": info["per_symbol_rows"],
        "window_bars": info["window_bars"],
    }
    (out_dir / "meta.json").write_text(json.dumps(meta))
    return meta


# --------------------------------------------------------------------------- #
# Reports                                                                     #
# --------------------------------------------------------------------------- #


class FoldReport(BaseModel):
    """Metrics + provenance for one walk-forward fold."""

    model_config = ConfigDict(frozen=True)

    index: int
    train_start_ts: int
    train_end_ts: int
    val_start_ts: int
    val_end_ts: int
    train_bars: int
    val_bars: int
    train_rows: int
    train_pos_rate: float
    n_fills: int
    fold_return_pct: float
    fold_sharpe: float | None = None
    fold_max_drawdown_pct: float | None = None
    final_equity: float
    starting_cash: float
    model_dir: str


class WalkForwardReport(BaseModel):
    """Aggregate OOS report stitched across all folds."""

    model_config = ConfigDict(frozen=True)

    n_folds: int
    n_oos_bars: int
    oos_total_return_pct: float
    oos_sharpe: float | None = None
    oos_max_drawdown_pct: float | None = None
    mean_fold_return_pct: float
    std_fold_return_pct: float
    mean_fold_sharpe: float | None = None
    std_fold_sharpe: float | None = None
    pct_folds_positive_return: float
    pct_folds_positive_sharpe: float | None = None
    folds: list[FoldReport] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _slice_bars_by_ts(
    bars_by_symbol: dict[str, list[BarEvent]], *, start_ns: int, end_ns: int
) -> dict[str, list[BarEvent]]:
    """Filter each symbol's bars to ``[start_ns, end_ns)``."""
    return {
        sym: [b for b in bars if start_ns <= b.ts_event < end_ns]
        for sym, bars in bars_by_symbol.items()
    }


def _canonical_timestamps(
    bars_by_symbol: dict[str, list[BarEvent]],
) -> list[int]:
    """Sorted union of all ts_event values across symbols."""
    seen: set[int] = set()
    for bars in bars_by_symbol.values():
        for b in bars:
            seen.add(b.ts_event)
    return sorted(seen)


def _sharpe_from_returns(returns: list[float], *, bars_per_year: int) -> float | None:
    """Annualised Sharpe; ``None`` if too few or zero-vol."""
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return None
    return (mean * bars_per_year) / (std * math.sqrt(bars_per_year))


def _max_drawdown_from_returns(returns: list[float]) -> float:
    """Max DD on the cumulative-return curve starting at 1.0."""
    if not returns:
        return 0.0
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in returns:
        equity *= 1.0 + r
        if equity > peak:
            peak = equity
        elif peak > 0:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _equity_returns(equity_curve: list[tuple[int, float]]) -> list[float]:
    rets: list[float] = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1][1]
        cur = equity_curve[i][1]
        if prev <= 0:
            rets.append(0.0)
        else:
            rets.append((cur - prev) / prev)
    return rets


def _safe_mean_std(values: Sequence[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    if len(values) < 2:
        return mean, 0.0
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return mean, math.sqrt(var)


# --------------------------------------------------------------------------- #
# Top-level coroutine                                                         #
# --------------------------------------------------------------------------- #


async def walk_forward_backtest(
    *,
    parquet_path: pathlib.Path | str,
    feature_names: Sequence[str],
    horizon_bars: int,
    bar_minutes: int,
    n_folds: int,
    train_min_bars: int,
    val_bars: int,
    purge_bars: int = 0,
    embargo_bars: int = 0,
    starting_cash: Decimal = Decimal("100000"),
    per_symbol_notional: Decimal = Decimal("10000"),
    venue: Venue = Venue.PAPER,
    asset_class: AssetClass = AssetClass.CRYPTO_SPOT,
    freq: str = "1m",
    cost_model: CostModel | None = None,
    risk_settings: Settings | None = None,
    out_dir: pathlib.Path | str | None = None,
    num_boost_round: int = 100,
    entry_threshold: float = 0.0,
    exit_threshold: float = 0.0,
) -> WalkForwardReport:
    """Train + backtest expanding-window folds; return aggregate report.

    Each fold:
      1. Trains a fresh GBM on ``bars[train_start..train_end)`` pooled
         across symbols and saves to ``out_dir/fold_<k>/``
         (``out_dir`` defaults to a tempdir if omitted).
      2. Backtests :class:`GBMStrategy` on ``bars[val_start..val_end)``
         with ``starting_cash`` as the initial NAV.
      3. Records per-fold return, Sharpe, drawdown, fill count.

    Per-fold return sequences are concatenated into the OOS series; the
    aggregate Sharpe / DD are computed on that series.  Each fold runs
    *independently* (positions don't carry) — see module docstring for
    the rationale.

    ``out_dir`` can be ``None`` for ad-hoc evaluation; otherwise model
    artifacts persist for inspection or re-use.
    """
    feature_list = list(feature_names)
    require_supported(feature_list)
    if n_folds < 1:
        raise ValueError(f"n_folds must be >= 1, got {n_folds}")

    bars_by_symbol = load_bars_from_parquet(
        parquet_path, venue=venue, asset_class=asset_class, freq=freq
    )
    if not bars_by_symbol:
        raise ValueError(f"parquet at {parquet_path} contains no rows")

    timestamps = _canonical_timestamps(bars_by_symbol)
    folds = _make_folds_local(
        len(timestamps),
        n_folds=n_folds,
        train_min_bars=train_min_bars,
        val_bars=val_bars,
        purge_bars=purge_bars,
        embargo_bars=embargo_bars,
    )

    out_dir_path: pathlib.Path | None = pathlib.Path(out_dir) if out_dir is not None else None
    bars_per_year = _bars_per_year_for_freq(freq)

    fold_reports: list[FoldReport] = []
    fold_returns_concat: list[float] = []
    fold_sharpes: list[float] = []
    fold_pct_returns: list[float] = []

    for fold in folds:
        train_start_ns = timestamps[fold.train_start]
        # train_end is exclusive in our index space; map to a ns boundary
        # by using the first *excluded* timestamp (or +1ns past the last
        # included if that puts us at the array end).
        train_end_ns = timestamps[fold.train_end - 1] + 1
        val_start_ns = timestamps[fold.val_start]
        val_end_ns = timestamps[fold.val_end - 1] + 1

        train_slice = _slice_bars_by_ts(
            bars_by_symbol, start_ns=train_start_ns, end_ns=train_end_ns
        )
        val_slice = _slice_bars_by_ts(bars_by_symbol, start_ns=val_start_ns, end_ns=val_end_ns)

        # Persist this fold's model in out_dir/fold_<k> if requested,
        # else use a tempdir scoped to the function.
        if out_dir_path is not None:
            fold_dir = out_dir_path / f"fold_{fold.index}"
        else:
            import tempfile

            fold_dir = pathlib.Path(tempfile.mkdtemp(prefix=f"wf_fold_{fold.index}_"))

        meta = _train_fold_model(
            train_bars_by_symbol=train_slice,
            feature_names=feature_list,
            horizon_bars=horizon_bars,
            bar_minutes=bar_minutes,
            out_dir=fold_dir,
            num_boost_round=num_boost_round,
        )

        # Run validation backtest on this fold's val slice.
        symbols = sorted(val_slice)
        flat_val = [b for bars in val_slice.values() for b in bars]
        if not flat_val:
            # No bars to validate on — record an empty fold and continue.
            fold_reports.append(
                FoldReport(
                    index=fold.index,
                    train_start_ts=train_start_ns,
                    train_end_ts=train_end_ns,
                    val_start_ts=val_start_ns,
                    val_end_ts=val_end_ns,
                    train_bars=fold.train_bars,
                    val_bars=fold.val_bars,
                    train_rows=int(meta["train_rows"]),
                    train_pos_rate=float(meta["train_pos_rate"]),
                    n_fills=0,
                    fold_return_pct=0.0,
                    final_equity=float(starting_cash),
                    starting_cash=float(starting_cash),
                    model_dir=str(fold_dir),
                )
            )
            continue

        engine_start_ns = min(b.ts_event for b in flat_val)
        engine_end_ns = max(b.ts_event for b in flat_val) + 1
        bar_reader = make_bar_reader(val_slice)
        datasource = BarsDataSource(
            symbols=symbols,
            freq=freq,
            start_ns=engine_start_ns,
            end_ns=engine_end_ns,
            bar_reader=bar_reader,
        )
        strategy = GBMStrategy(
            symbols=symbols,
            model_dir=fold_dir,
            bar_minutes=bar_minutes,
            entry_threshold=entry_threshold,
            exit_threshold=exit_threshold,
            per_symbol_notional=per_symbol_notional,
            venue=venue,
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

        report = compute_metrics(blotter, bars_per_year=bars_per_year)
        equity_curve = [(ts, float(eq)) for ts, eq in blotter.equity_curve]
        fold_returns = _equity_returns(equity_curve)
        fold_returns_concat.extend(fold_returns)
        if report.sharpe is not None:
            fold_sharpes.append(report.sharpe)
        fold_pct_returns.append(report.total_return_pct)

        fold_reports.append(
            FoldReport(
                index=fold.index,
                train_start_ts=train_start_ns,
                train_end_ts=train_end_ns,
                val_start_ts=val_start_ns,
                val_end_ts=val_end_ns,
                train_bars=fold.train_bars,
                val_bars=fold.val_bars,
                train_rows=int(meta["train_rows"]),
                train_pos_rate=float(meta["train_pos_rate"]),
                n_fills=int(report.n_fills),
                fold_return_pct=float(report.total_return_pct),
                fold_sharpe=report.sharpe,
                fold_max_drawdown_pct=report.max_drawdown_pct,
                final_equity=float(report.final_equity),
                starting_cash=float(starting_cash),
                model_dir=str(fold_dir),
            )
        )

    # Aggregate
    oos_sharpe = _sharpe_from_returns(fold_returns_concat, bars_per_year=bars_per_year)
    oos_max_dd = _max_drawdown_from_returns(fold_returns_concat)
    if fold_returns_concat:
        oos_total_return = math.prod(1.0 + r for r in fold_returns_concat) - 1.0
    else:
        oos_total_return = 0.0
    mean_ret, std_ret = _safe_mean_std(fold_pct_returns)
    mean_sh: float | None
    std_sh: float | None
    if fold_sharpes:
        mean_sh, std_sh = _safe_mean_std(fold_sharpes)
    else:
        mean_sh = None
        std_sh = None
    pct_pos_ret = (
        sum(1 for r in fold_pct_returns if r > 0) / len(fold_pct_returns)
        if fold_pct_returns
        else 0.0
    )
    pct_pos_sh: float | None = (
        sum(1 for s in fold_sharpes if s > 0) / len(fold_sharpes) if fold_sharpes else None
    )

    return WalkForwardReport(
        n_folds=len(fold_reports),
        n_oos_bars=len(fold_returns_concat),
        oos_total_return_pct=oos_total_return * 100.0,
        oos_sharpe=oos_sharpe,
        oos_max_drawdown_pct=oos_max_dd * 100.0 if oos_max_dd > 0 else 0.0,
        mean_fold_return_pct=mean_ret,
        std_fold_return_pct=std_ret,
        mean_fold_sharpe=mean_sh,
        std_fold_sharpe=std_sh,
        pct_folds_positive_return=pct_pos_ret,
        pct_folds_positive_sharpe=pct_pos_sh,
        folds=fold_reports,
        config={
            "feature_names": feature_list,
            "horizon_bars": horizon_bars,
            "bar_minutes": bar_minutes,
            "n_folds": n_folds,
            "train_min_bars": train_min_bars,
            "val_bars": val_bars,
            "purge_bars": purge_bars,
            "embargo_bars": embargo_bars,
            "starting_cash": float(starting_cash),
            "per_symbol_notional": float(per_symbol_notional),
            "venue": str(venue),
            "asset_class": str(asset_class),
            "freq": freq,
            "num_boost_round": num_boost_round,
            "entry_threshold": entry_threshold,
            "exit_threshold": exit_threshold,
        },
    )
