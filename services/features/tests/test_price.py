"""Tests for features.transforms.price — returns + momentum."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from features.transforms.price import PriceFeatures


def test_first_bar_emits_all_none() -> None:
    p = PriceFeatures()
    out = p.update(Decimal("100"))
    assert set(out) == {"ret_log_1", "ret_simple_1", "mom_5", "mom_20", "mom_60"}
    assert all(v is None for v in out.values())


def test_log_return_matches_math_log() -> None:
    p = PriceFeatures()
    p.update(Decimal("100"))
    out = p.update(Decimal("110"))
    assert out["ret_log_1"] is not None
    assert math.isclose(out["ret_log_1"], math.log(1.1), rel_tol=1e-12)


def test_simple_return_matches_ratio_minus_one() -> None:
    p = PriceFeatures()
    p.update(Decimal("100"))
    out = p.update(Decimal("110"))
    assert out["ret_simple_1"] == pytest.approx(0.10)


def test_momentum_requires_k_plus_one_bars() -> None:
    p = PriceFeatures()
    out: dict[str, float | None] = {}
    # 5 bars total: not enough for mom_5 (need 6).
    for px in (100, 101, 102, 103, 104):
        out = p.update(Decimal(str(px)))
    assert out["mom_5"] is None

    # 6th bar enables mom_5: (105/100) - 1 = 0.05.
    out = p.update(Decimal("105"))
    assert out["mom_5"] == pytest.approx(0.05)
    # mom_20 still requires 21 bars; remains None.
    assert out["mom_20"] is None


def test_zero_previous_close_emits_none_rather_than_dividing_by_zero() -> None:
    p = PriceFeatures()
    p.update(Decimal("0"))
    out = p.update(Decimal("100"))
    assert out["ret_log_1"] is None
    assert out["ret_simple_1"] is None


def test_max_lookback_must_cover_longest_momentum() -> None:
    with pytest.raises(ValueError, match="max_lookback"):
        PriceFeatures(max_lookback=10, momentum_lookbacks=(20,))
