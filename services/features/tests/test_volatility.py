"""Tests for features.transforms.volatility — realized vol + Parkinson + GK."""

from __future__ import annotations

import math
import statistics
from decimal import Decimal

import pytest

from features.transforms.volatility import VolatilityFeatures


def _const_bar(price: str = "100") -> tuple[Decimal, Decimal, Decimal, Decimal]:
    p = Decimal(price)
    return p, p, p, p


def test_bootstrap_emits_all_none() -> None:
    v = VolatilityFeatures(windows=(5,))
    out = v.update(Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), None)
    assert out["vol_rs_5"] is None
    assert out["vol_park_5"] is None
    assert out["vol_gk_5"] is None


def test_constant_prices_yield_zero_realized_and_parkinson_vol() -> None:
    v = VolatilityFeatures(windows=(5,))
    out: dict[str, float | None] = {}
    for _ in range(7):
        out = v.update(*_const_bar(), 0.0)
    assert out["vol_rs_5"] == 0.0
    assert out["vol_park_5"] == 0.0
    # GK on a perfectly flat bar is zero in numerator → returns None
    # by design (we never sqrt a non-positive accumulator).
    assert out["vol_gk_5"] is None


def test_realized_vol_matches_statistics_stdev() -> None:
    v = VolatilityFeatures(windows=(5,))
    rets = [0.01, -0.02, 0.03, -0.01, 0.02]
    out: dict[str, float | None] = {}
    # Bar 0: bootstrap (no log_ret).
    v.update(Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), None)
    # Bars 1-5: feed each ret with a benign OHLC.
    for r in rets:
        out = v.update(Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), r)
    expected = statistics.stdev(rets)
    assert out["vol_rs_5"] is not None
    assert math.isclose(out["vol_rs_5"], expected, rel_tol=1e-12)


def test_parkinson_positive_for_real_bars() -> None:
    v = VolatilityFeatures(windows=(3,))
    bars = [
        (100, 105, 99, 102),
        (102, 106, 100, 104),
        (104, 108, 103, 107),
    ]
    out: dict[str, float | None] = {}
    for o, h, lo, c in bars:
        out = v.update(Decimal(o), Decimal(h), Decimal(lo), Decimal(c), 0.0)
    assert out["vol_park_3"] is not None
    assert out["vol_park_3"] > 0


def test_garman_klass_positive_for_real_bars() -> None:
    v = VolatilityFeatures(windows=(3,))
    bars = [
        (100, 105, 99, 102),
        (102, 107, 101, 105),
        (105, 109, 104, 108),
    ]
    out: dict[str, float | None] = {}
    for o, h, lo, c in bars:
        out = v.update(Decimal(o), Decimal(h), Decimal(lo), Decimal(c), 0.0)
    assert out["vol_gk_3"] is not None
    assert out["vol_gk_3"] > 0


def test_gk_returns_none_when_close_to_open_dominates() -> None:
    """Pathological bars where ln(C/O)^2 >> ln(H/L)^2 produce a negative
    GK accumulator; we emit None rather than sqrt(-x)."""
    v = VolatilityFeatures(windows=(2,))
    # Tiny H-L range, large C-O move → GK accumulator is negative.
    v.update(Decimal("100"), Decimal("100.1"), Decimal("99.9"), Decimal("110"), 0.0)
    out = v.update(Decimal("110"), Decimal("110.1"), Decimal("109.9"), Decimal("100"), 0.0)
    assert out["vol_gk_2"] is None


@pytest.mark.parametrize("windows", [(), (0,), (-1,)])
def test_invalid_windows_raise_at_construction(windows: tuple[int, ...]) -> None:
    with pytest.raises(ValueError):
        VolatilityFeatures(windows=windows)
