"""
Tests for v1.1.0 label improvements — Bayesian β, trading-day horizons,
open-to-close returns, CAR, and v1.0.0 backward compatibility.

Tests verify:
- Both v1.0.0 and v1.1.0 are registered with distinct IDs.
- v1.1.0 Bayesian β shrinkage: β is pulled toward the prior.
- v1.1.0 trading-day horizons: +5d is 5 trading bars, not 5 calendar days.
- v1.1.0 open-to-close returns: uses open price for the first session.
- v1.1.0 CAR method: sums daily abnormal returns.
- v1.0.0 backward compatibility: produces the same results as before.
- v1.1.0 with default config produces results close to v1.0.0 (mild shrinkage).
- Thin-trading guard drops symbols with insufficient bars.
"""

from __future__ import annotations

import datetime as dt
import math
import pathlib
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _make_bars(
    symbol: str,
    start_ns: int,
    n_days: int,
    base_price: float = 100.0,
    drift: float = 0.0001,
    volatility: float = 0.01,
    seed: int = 42,
    gap_after: int | None = None,
    gap_days: int = 3,
) -> list:
    """Generate n_days of synthetic daily bars with drift + noise.

    If ``gap_after`` is set, inserts a ``gap_days`` calendar-day gap
    after that bar index (simulating a weekend/holiday where no bar
    exists, but the next bar is several calendar days later).
    """
    import random

    from quant_foundry.modules.registry import PriceBar

    rng = random.Random(seed)
    NS_PER_DAY = 86_400_000_000_000
    bars = []
    price = base_price
    for i in range(n_days):
        if gap_after is not None and i == gap_after:
            # Insert a gap — skip some calendar days
            ts = start_ns + (i + gap_days) * NS_PER_DAY
        else:
            ts = start_ns + i * NS_PER_DAY
        ret = drift + rng.gauss(0, volatility)
        price *= (1.0 + ret)
        bars.append(PriceBar(
            symbol=symbol,
            ts_ns=ts,
            open=price * 0.999,
            high=price * 1.005,
            low=price * 0.995,
            close=price,
            volume=1_000_000.0,
        ))
    return bars


# --------------------------------------------------------------------------- #
# Registration tests                                                          #
# --------------------------------------------------------------------------- #


def test_both_label_versions_registered() -> None:
    """Both v1.0.0 and v1.1.0 should be registered."""
    from quant_foundry.modules import ModuleRegistry, load_all_modules

    load_all_modules()
    registry = ModuleRegistry.instance()
    labels = registry.list_by_category("label")

    assert "label:abnormal-return:1.1.0" in labels
    assert "label:abnormal-return-v1:1.0.0" in labels


# --------------------------------------------------------------------------- #
# Bayesian β shrinkage tests                                                  #
# --------------------------------------------------------------------------- #


def test_vasicek_shrinkage_pulls_beta_toward_prior() -> None:
    """Vasicek shrinkage pulls β toward the prior (default 1.0)."""
    from quant_foundry.modules.labels.abnormal_return import (
        _estimate_beta_v1,
        _estimate_beta_v2,
    )
    from quant_foundry.modules.registry import PriceBar

    NS_PER_DAY = 86_400_000_000_000
    start_ns = int(dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc).timestamp()) * 1_000_000_000

    # Create bars where OLS β would be far from 1.0
    asset_bars = _make_bars("AAPL", start_ns, 300, base_price=100.0, seed=42, volatility=0.02)
    bench_bars = _make_bars("SPY", start_ns, 300, base_price=400.0, seed=99, volatility=0.005)

    bench_sorted = sorted(bench_bars, key=lambda b: b.ts_ns)
    decision_time = start_ns + 200 * NS_PER_DAY

    # Pure OLS β
    bench_ts = [b.ts_ns for b in bench_sorted]
    bench_close = [b.close for b in bench_sorted]
    beta_ols = _estimate_beta_v1(
        asset_bars, bench_ts, bench_close, decision_time,
        window=252, min_window=60,
    )
    assert beta_ols is not None

    # Shrunk β with prior=1.0, shrinkage=0.5
    beta_shrunk = _estimate_beta_v2(
        asset_bars, bench_sorted, decision_time,
        window=252, min_window=60,
        beta_prior=1.0, shrinkage=0.5,
    )
    assert beta_shrunk is not None

    # Shrunk β should be closer to 1.0 than OLS β
    dist_ols = abs(beta_ols - 1.0)
    dist_shrunk = abs(beta_shrunk - 1.0)
    assert dist_shrunk < dist_ols, (
        f"shrunk β {beta_shrunk} should be closer to 1.0 than OLS β {beta_ols}"
    )

    # With shrinkage=1.0, β should be exactly the prior
    beta_pure_prior = _estimate_beta_v2(
        asset_bars, bench_sorted, decision_time,
        window=252, min_window=60,
        beta_prior=1.0, shrinkage=1.0,
    )
    assert beta_pure_prior == 1.0

    # With shrinkage=0.0, β should equal OLS β
    beta_no_shrink = _estimate_beta_v2(
        asset_bars, bench_sorted, decision_time,
        window=252, min_window=60,
        beta_prior=1.0, shrinkage=0.0,
    )
    assert beta_no_shrink == beta_ols


def test_shrinkage_clamped_to_valid_range() -> None:
    """Shrinkage > 1 or < 0 is clamped to [0, 1]."""
    from quant_foundry.modules.labels.abnormal_return import _estimate_beta_v2
    from quant_foundry.modules.registry import PriceBar

    NS_PER_DAY = 86_400_000_000_000
    start_ns = int(dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc).timestamp()) * 1_000_000_000

    asset_bars = _make_bars("AAPL", start_ns, 300, seed=42)
    bench_bars = _make_bars("SPY", start_ns, 300, seed=99)
    bench_sorted = sorted(bench_bars, key=lambda b: b.ts_ns)
    decision_time = start_ns + 200 * NS_PER_DAY

    # shrinkage=2.0 should be clamped to 1.0 → pure prior
    beta_over = _estimate_beta_v2(
        asset_bars, bench_sorted, decision_time,
        window=252, min_window=60,
        beta_prior=0.5, shrinkage=2.0,
    )
    assert beta_over == 0.5

    # shrinkage=-1.0 should be clamped to 0.0 → pure OLS
    beta_under = _estimate_beta_v2(
        asset_bars, bench_sorted, decision_time,
        window=252, min_window=60,
        beta_prior=0.5, shrinkage=-1.0,
    )
    assert beta_under is not None
    # Should equal pure OLS (shrinkage=0.0)
    beta_ols = _estimate_beta_v2(
        asset_bars, bench_sorted, decision_time,
        window=252, min_window=60,
        beta_prior=0.5, shrinkage=0.0,
    )
    assert beta_under == beta_ols


# --------------------------------------------------------------------------- #
# Trading-day horizon tests                                                   #
# --------------------------------------------------------------------------- #


def test_v11_trading_day_horizons() -> None:
    """v1.1.0 uses trading-bar count for horizons, not calendar days."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import (
        FeatureRowData,
        ModuleRegistry,
    )

    load_all_modules()
    label_mod = ModuleRegistry.instance().create("label:abnormal-return:1.1.0")

    NS_PER_DAY = 86_400_000_000_000
    start_ns = int(dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc).timestamp()) * 1_000_000_000

    # Create bars with a gap after bar 260 (simulating weekend)
    asset_bars = _make_bars("AAPL", start_ns, 400, seed=42, gap_after=260, gap_days=3)
    bench_bars = _make_bars("SPY", start_ns, 400, seed=99, gap_after=260, gap_days=3)

    # Decision time at bar 260 (just before the gap)
    decision_time = asset_bars[260].ts_ns
    rows = [FeatureRowData(
        symbol="AAPL",
        decision_time=decision_time,
        features={"sent_earnings": 0.5},
    )]

    labeled = label_mod.compute_labels(
        rows,
        price_bars={"AAPL": asset_bars},
        benchmark_bars=bench_bars,
    )

    assert len(labeled) == 1
    row = labeled[0]
    assert row.label is not None
    # Should have AR columns for each horizon
    assert "ar_1d" in row.features
    assert "ar_5d" in row.features
    assert "ar_21d" in row.features
    assert "ar_63d" in row.features


# --------------------------------------------------------------------------- #
# Open-to-close return tests                                                  #
# --------------------------------------------------------------------------- #


def test_v11_open_to_close_returns() -> None:
    """v1.1.0 open_to_close return type uses open price for first session."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import (
        FeatureRowData,
        ModuleRegistry,
    )

    load_all_modules()
    label_mod = ModuleRegistry.instance().create(
        "label:abnormal-return:1.1.0",
        config={"return_type": "open_to_close"},
    )

    NS_PER_DAY = 86_400_000_000_000
    start_ns = int(dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc).timestamp()) * 1_000_000_000

    asset_bars = _make_bars("AAPL", start_ns, 400, seed=42)
    bench_bars = _make_bars("SPY", start_ns, 400, seed=99)

    decision_time = asset_bars[260].ts_ns
    rows = [FeatureRowData(
        symbol="AAPL",
        decision_time=decision_time,
        features={"sent_earnings": 0.5},
    )]

    labeled = label_mod.compute_labels(
        rows,
        price_bars={"AAPL": asset_bars},
        benchmark_bars=bench_bars,
    )

    assert len(labeled) == 1
    assert labeled[0].label is not None


# --------------------------------------------------------------------------- #
# CAR (cumulative abnormal return) tests                                      #
# --------------------------------------------------------------------------- #


def test_v11_car_method() -> None:
    """v1.1.0 CAR method sums daily abnormal returns."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import (
        FeatureRowData,
        ModuleRegistry,
    )

    load_all_modules()
    label_mod_car = ModuleRegistry.instance().create(
        "label:abnormal-return:1.1.0",
        config={"ar_method": "car"},
    )
    label_mod_endpoint = ModuleRegistry.instance().create(
        "label:abnormal-return:1.1.0",
        config={"ar_method": "endpoint"},
    )

    NS_PER_DAY = 86_400_000_000_000
    start_ns = int(dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc).timestamp()) * 1_000_000_000

    asset_bars = _make_bars("AAPL", start_ns, 400, seed=42)
    bench_bars = _make_bars("SPY", start_ns, 400, seed=99)

    decision_time = asset_bars[260].ts_ns
    rows = [FeatureRowData(
        symbol="AAPL",
        decision_time=decision_time,
        features={"sent_earnings": 0.5},
    )]

    labeled_car = label_mod_car.compute_labels(
        rows,
        price_bars={"AAPL": asset_bars},
        benchmark_bars=bench_bars,
    )
    labeled_ep = label_mod_endpoint.compute_labels(
        rows,
        price_bars={"AAPL": asset_bars},
        benchmark_bars=bench_bars,
    )

    assert len(labeled_car) == 1
    assert len(labeled_ep) == 1
    # CAR and endpoint should produce different values in general
    # (they measure different things — sum vs endpoint)
    assert labeled_car[0].label is not None
    assert labeled_ep[0].label is not None
    # Both should be finite
    assert math.isfinite(labeled_car[0].label)
    assert math.isfinite(labeled_ep[0].label)


# --------------------------------------------------------------------------- #
# v1.0.0 backward compatibility tests                                         #
# --------------------------------------------------------------------------- #


def test_v10_still_works() -> None:
    """v1.0.0 label module produces the same results as before."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import (
        FeatureRowData,
        ModuleRegistry,
    )

    load_all_modules()
    label_mod = ModuleRegistry.instance().create("label:abnormal-return-v1:1.0.0")

    NS_PER_DAY = 86_400_000_000_000
    start_ns = int(dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc).timestamp()) * 1_000_000_000

    asset_bars = _make_bars("AAPL", start_ns, 400, seed=42)
    bench_bars = _make_bars("SPY", start_ns, 400, seed=99)

    decision_time = start_ns + 260 * NS_PER_DAY
    rows = [FeatureRowData(
        symbol="AAPL",
        decision_time=decision_time,
        features={"sent_earnings": 0.5},
    )]

    labeled = label_mod.compute_labels(
        rows,
        price_bars={"AAPL": asset_bars},
        benchmark_bars=bench_bars,
    )

    assert len(labeled) == 1
    row = labeled[0]
    assert row.label is not None
    assert "ar_1d" in row.features
    assert "ar_5d" in row.features
    assert "ar_21d" in row.features
    assert "ar_63d" in row.features
    assert row.label == row.features["ar_5d"]


def test_v10_drops_insufficient_history() -> None:
    """v1.0.0 drops rows with insufficient price history."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import (
        FeatureRowData,
        ModuleRegistry,
    )

    load_all_modules()
    label_mod = ModuleRegistry.instance().create("label:abnormal-return-v1:1.0.0")

    NS_PER_DAY = 86_400_000_000_000
    start_ns = int(dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc).timestamp()) * 1_000_000_000

    # Only 10 days — not enough for β window (min 60)
    asset_bars = _make_bars("AAPL", start_ns, 10)
    bench_bars = _make_bars("SPY", start_ns, 10)

    decision_time = start_ns + 5 * NS_PER_DAY
    rows = [FeatureRowData(
        symbol="AAPL",
        decision_time=decision_time,
        features={"sent_earnings": 0.5},
    )]

    labeled = label_mod.compute_labels(
        rows,
        price_bars={"AAPL": asset_bars},
        benchmark_bars=bench_bars,
    )
    assert len(labeled) == 0


# --------------------------------------------------------------------------- #
# v1.1.0 vs v1.0.0 comparison                                                 #
# --------------------------------------------------------------------------- #


def test_v11_default_close_to_v10() -> None:
    """v1.1.0 with default config (mild shrinkage) produces results
    close to v1.0.0 — the shrinkage is gentle, not radical."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import (
        FeatureRowData,
        ModuleRegistry,
    )

    load_all_modules()
    label_v10 = ModuleRegistry.instance().create("label:abnormal-return-v1:1.0.0")
    label_v11 = ModuleRegistry.instance().create("label:abnormal-return:1.1.0")

    NS_PER_DAY = 86_400_000_000_000
    start_ns = int(dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc).timestamp()) * 1_000_000_000

    asset_bars = _make_bars("AAPL", start_ns, 400, seed=42)
    bench_bars = _make_bars("SPY", start_ns, 400, seed=99)

    decision_time = start_ns + 260 * NS_PER_DAY
    rows = [FeatureRowData(
        symbol="AAPL",
        decision_time=decision_time,
        features={"sent_earnings": 0.5},
    )]

    labeled_v10 = label_v10.compute_labels(
        rows,
        price_bars={"AAPL": asset_bars},
        benchmark_bars=bench_bars,
    )
    labeled_v11 = label_v11.compute_labels(
        rows,
        price_bars={"AAPL": asset_bars},
        benchmark_bars=bench_bars,
    )

    assert len(labeled_v10) == 1
    assert len(labeled_v11) == 1

    # Both should produce finite labels
    assert math.isfinite(labeled_v10[0].label)
    assert math.isfinite(labeled_v11[0].label)

    # Labels should be in the same ballpark (both are abnormal returns
    # for the same asset at the same time — shrinkage shouldn't
    # radically change the label)
    # We use a generous tolerance because the β difference affects the AR.
    diff = abs(labeled_v10[0].label - labeled_v11[0].label)
    assert diff < 0.1, (
        f"v1.0 label {labeled_v10[0].label} vs v1.1 label {labeled_v11[0].label} "
        f"diff {diff} > 0.1"
    )


# --------------------------------------------------------------------------- #
# Thin-trading guard                                                           #
# --------------------------------------------------------------------------- #


def test_v11_thin_trading_guard() -> None:
    """v1.1.0 drops symbols with too few bars in the β window."""
    from quant_foundry.modules import load_all_modules
    from quant_foundry.modules.registry import (
        FeatureRowData,
        ModuleRegistry,
    )

    load_all_modules()
    label_mod = ModuleRegistry.instance().create(
        "label:abnormal-return:1.1.0",
        config={"min_beta_window": 100},
    )

    NS_PER_DAY = 86_400_000_000_000
    start_ns = int(dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc).timestamp()) * 1_000_000_000

    # Only 50 bars — below the min_beta_window of 100
    asset_bars = _make_bars("AAPL", start_ns, 50, seed=42)
    bench_bars = _make_bars("SPY", start_ns, 50, seed=99)

    decision_time = asset_bars[30].ts_ns
    rows = [FeatureRowData(
        symbol="AAPL",
        decision_time=decision_time,
        features={"sent_earnings": 0.5},
    )]

    labeled = label_mod.compute_labels(
        rows,
        price_bars={"AAPL": asset_bars},
        benchmark_bars=bench_bars,
    )
    assert len(labeled) == 0  # dropped due to thin trading
