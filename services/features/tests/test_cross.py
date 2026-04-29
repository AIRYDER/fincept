"""Tests for features.transforms.cross — rolling beta + correlation."""

from __future__ import annotations

import math

import pytest

from features.transforms.cross import CrossFeatures


def test_bootstrap_emits_all_none() -> None:
    c = CrossFeatures(benchmark_symbol="B", windows=(5,))
    out = c.on_symbol_ret("S", 0.01)
    assert out == {"beta_B_5": None, "corr_B_5": None}


def test_perfect_correlation_yields_beta_and_corr_one() -> None:
    """When the symbol IS the benchmark, beta = 1 and corr = 1 exactly."""
    c = CrossFeatures(benchmark_symbol="B", windows=(5,))
    rs = [0.01, -0.02, 0.03, -0.01, 0.02]
    out: dict[str, float | None] = {}
    for r in rs:
        c.on_benchmark_ret(r)
        out = c.on_symbol_ret("B", r)
    assert out["beta_B_5"] is not None
    assert out["corr_B_5"] is not None
    assert math.isclose(out["beta_B_5"], 1.0, rel_tol=1e-12)
    assert math.isclose(out["corr_B_5"], 1.0, rel_tol=1e-12)


def test_beta_is_two_when_symbol_returns_are_double_benchmark() -> None:
    """y = 2x → beta = 2.0, corr = 1.0 exactly."""
    c = CrossFeatures(benchmark_symbol="B", windows=(5,))
    bench = [0.01, -0.02, 0.03, -0.01, 0.02]
    sym = [r * 2 for r in bench]
    out: dict[str, float | None] = {}
    for b, s in zip(bench, sym, strict=True):
        c.on_benchmark_ret(b)
        out = c.on_symbol_ret("S", s)
    assert out["beta_B_5"] is not None
    assert out["corr_B_5"] is not None
    assert math.isclose(out["beta_B_5"], 2.0, rel_tol=1e-12)
    assert math.isclose(out["corr_B_5"], 1.0, rel_tol=1e-12)


def test_no_benchmark_history_keeps_outputs_none_even_with_symbol_rets() -> None:
    c = CrossFeatures(benchmark_symbol="B", windows=(5,))
    out: dict[str, float | None] = {}
    for r in [0.01, -0.02, 0.03, -0.01, 0.02]:
        out = c.on_symbol_ret("S", r)
    assert out["beta_B_5"] is None
    assert out["corr_B_5"] is None


def test_corr_returns_none_when_benchmark_variance_is_zero() -> None:
    """Constant benchmark → variance = 0 → beta and corr are mathematically
    undefined; we emit None rather than NaN."""
    c = CrossFeatures(benchmark_symbol="B", windows=(5,))
    out: dict[str, float | None] = {}
    for _ in range(5):
        c.on_benchmark_ret(0.01)  # constant
        out = c.on_symbol_ret("S", 0.02)
    assert out["beta_B_5"] is None
    assert out["corr_B_5"] is None


@pytest.mark.parametrize("windows", [(), (1,)])
def test_invalid_windows_raise(windows: tuple[int, ...]) -> None:
    with pytest.raises(ValueError):
        CrossFeatures(benchmark_symbol="B", windows=windows)
