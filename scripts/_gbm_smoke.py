"""scripts/_gbm_smoke.py — train a tiny OHLCV-only GBM model, then
backtest it end-to-end on real SPY+AAPL bars.  Demonstrates the full
ingest -> train -> backtest pipeline."""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from decimal import Decimal

import lightgbm as lgb
import numpy as np

# Make services importable without packaging the script.
_REPO = pathlib.Path(__file__).resolve().parent.parent
for _src in (
    _REPO / "services" / "backtester" / "src",
    _REPO / "libs" / "fincept-core" / "src",
):
    sys.path.insert(0, str(_src))

from backtester.runner import load_bars_from_parquet, run_backtest  # noqa: E402
from fincept_core.schemas import AssetClass, Venue  # noqa: E402

PARQUET = _REPO / "data" / "real_eq_2024h1.parquet"
MODEL_DIR = _REPO / "models" / "gbm_smoke"
FEATURES = ["ret_1m", "ret_5m", "rv_5m", "mom_z_5m"]
HORIZON = 5  # bars ahead


def train(bars_by_symbol: dict[str, list]) -> None:
    """Train a tiny lightgbm Booster on a hand-built feature matrix
    derived from real OHLCV.  Saved to MODEL_DIR/model.txt + meta.json."""
    rows: list[list[float]] = []
    labels: list[int] = []
    for _sym, bars in bars_by_symbol.items():
        closes = np.array([float(b.close) for b in bars])
        if len(closes) < HORIZON + 6:
            continue
        log_rets = np.log(closes[1:] / closes[:-1])
        for i in range(5, len(closes) - HORIZON):
            ret_1m = float(log_rets[i - 1])
            ret_5m = float(np.log(closes[i] / closes[i - 5]))
            rv_5m = float(np.std(log_rets[i - 5 : i], ddof=0))
            recent = log_rets[i - 5 : i]
            mu = float(np.sum(recent))
            sd = rv_5m
            mom_z = mu / (sd * np.sqrt(5)) if sd > 0 else 0.0
            future = float(np.log(closes[i + HORIZON] / closes[i]))
            rows.append([ret_1m, ret_5m, rv_5m, mom_z])
            labels.append(1 if future > 0 else 0)

    x = np.array(rows, dtype=np.float64)
    y = np.array(labels, dtype=np.int32)
    print(f"[train] rows={len(rows)}  pos_rate={y.mean():.3f}")

    booster = lgb.train(
        params={
            "objective": "binary",
            "metric": "binary_logloss",
            "verbosity": -1,
            "num_leaves": 7,
            "learning_rate": 0.05,
        },
        train_set=lgb.Dataset(x, label=y, feature_name=FEATURES),
        num_boost_round=100,
    )
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(MODEL_DIR / "model.txt"))
    (MODEL_DIR / "meta.json").write_text(
        json.dumps(
            {
                "features": FEATURES,
                "horizon_bars": HORIZON,
                "horizon_ns": HORIZON * 86_400_000_000_000,
                "trained_at": 0,
            }
        )
    )
    print(f"[train] saved model to {MODEL_DIR}")


async def main() -> None:
    if not PARQUET.exists():
        sys.exit(
            f"missing {PARQUET}; run scripts/ingest_bars.py first to generate it"
        )
    bars_by_symbol = load_bars_from_parquet(
        PARQUET, venue=Venue.NASDAQ, asset_class=AssetClass.EQUITY, freq="1d"
    )
    train(bars_by_symbol)

    print("[backtest] running gbm strategy on real bars...")
    result = await run_backtest(
        parquet_path=PARQUET,
        strategy_name="gbm",
        strategy_params={
            "model_dir": str(MODEL_DIR),
            "bar_minutes": 60 * 24,  # 1 day
            "per_symbol_notional": Decimal("10000"),
        },
        starting_cash=Decimal("100000"),
        venue=Venue.NASDAQ,
        asset_class=AssetClass.EQUITY,
        freq="1d",
        persist=False,
    )
    r = result.report
    print(f"  n_bars        : {r.n_bars}")
    print(f"  n_fills       : {r.n_fills}")
    print(f"  final_equity  : {r.final_equity:,.2f} USD")
    print(f"  total_return  : {r.total_return_pct:+.2f}%")
    sharpe = f"{r.sharpe:.2f}" if r.sharpe is not None else "n/a"
    print(f"  sharpe        : {sharpe}")
    print(f"  max_drawdown  : {r.max_drawdown_pct:.2f}%")


if __name__ == "__main__":
    asyncio.run(main())
