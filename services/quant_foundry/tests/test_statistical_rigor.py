"""
Tests for statistical rigor — significance tests, multi-seed runner,
and placebo tests.

Tests verify:
- Diebold-Mariano test: basic computation, A better, B better, tie,
  length mismatch error.
- Bootstrap Sharpe CI: basic CI, confidence level width, reproducibility.
- Bootstrap Sharpe difference CI: significant and not-significant cases.
- MultiSeedRunner: mock harness, n_seeds results, mean/std computation.
- PlaceboTest: basic p-value, feature permutation importance.
- ComparisonReport.significance_test with two lists of BenchmarkResult.
"""

from __future__ import annotations

import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# --------------------------------------------------------------------------- #
# Diebold-Mariano test tests                                                   #
# --------------------------------------------------------------------------- #


def test_diebold_mariano_basic() -> None:
    """DM test computes dm_stat, p_value in [0,1], better_model is correct."""
    from quant_foundry.modules.benchmark.significance import diebold_mariano_test

    errors_a = [0.1, -0.2, 0.05, 0.3, -0.1, 0.15, 0.2, -0.05]
    errors_b = [0.5, -0.6, 0.4, 0.7, -0.5, 0.55, 0.6, -0.45]

    result = diebold_mariano_test(errors_a, errors_b)

    assert "dm_stat" in result
    assert "p_value" in result
    assert "significant_at_5pct" in result
    assert "better_model" in result

    assert isinstance(result["dm_stat"], float)
    assert 0.0 <= result["p_value"] <= 1.0
    assert isinstance(result["significant_at_5pct"], bool)
    # A has smaller errors → A is better
    assert result["better_model"] == "a"


def test_diebold_mariano_a_better() -> None:
    """When errors_a are consistently smaller, better_model should be 'a'."""
    from quant_foundry.modules.benchmark.significance import diebold_mariano_test

    # A has tiny errors, B has large errors
    errors_a = [
        0.01,
        0.02,
        0.01,
        0.03,
        0.02,
        0.01,
        0.02,
        0.03,
        0.01,
        0.02,
        0.01,
        0.03,
        0.02,
        0.01,
        0.02,
        0.03,
    ]
    errors_b = [0.5, 0.6, 0.5, 0.7, 0.6, 0.5, 0.6, 0.7, 0.5, 0.6, 0.5, 0.7, 0.6, 0.5, 0.6, 0.7]

    result = diebold_mariano_test(errors_a, errors_b)
    assert result["better_model"] == "a"
    assert result["significant_at_5pct"] is True


def test_diebold_mariano_b_better() -> None:
    """When errors_b are consistently smaller, better_model should be 'b'."""
    from quant_foundry.modules.benchmark.significance import diebold_mariano_test

    errors_a = [0.5, 0.6, 0.5, 0.7, 0.6, 0.5, 0.6, 0.7, 0.5, 0.6, 0.5, 0.7, 0.6, 0.5, 0.6, 0.7]
    errors_b = [
        0.01,
        0.02,
        0.01,
        0.03,
        0.02,
        0.01,
        0.02,
        0.03,
        0.01,
        0.02,
        0.01,
        0.03,
        0.02,
        0.01,
        0.02,
        0.03,
    ]

    result = diebold_mariano_test(errors_a, errors_b)
    assert result["better_model"] == "b"
    assert result["significant_at_5pct"] is True


def test_diebold_mariano_tie() -> None:
    """When errors are identical, should be a tie (p_value >= 0.05)."""
    from quant_foundry.modules.benchmark.significance import diebold_mariano_test

    errors = [0.1, 0.2, 0.15, 0.3, 0.25, 0.1, 0.2, 0.15]

    result = diebold_mariano_test(errors, errors)
    assert result["better_model"] == "tie"
    assert result["p_value"] >= 0.05
    assert result["significant_at_5pct"] is False


def test_diebold_mariano_length_mismatch() -> None:
    """Different length error lists should raise ValueError."""
    from quant_foundry.modules.benchmark.significance import diebold_mariano_test

    with pytest.raises(ValueError, match="same length"):
        diebold_mariano_test([0.1, 0.2, 0.3], [0.1, 0.2])


# --------------------------------------------------------------------------- #
# Bootstrap Sharpe CI tests                                                    #
# --------------------------------------------------------------------------- #


def test_bootstrap_sharpe_ci_basic() -> None:
    """Bootstrap CI computes mean, std, lower, upper; lower < upper; CI contains mean."""
    from quant_foundry.modules.benchmark.significance import bootstrap_sharpe_ci

    sharpe_ratios = [
        1.2,
        1.1,
        1.3,
        1.0,
        1.2,
        1.15,
        1.25,
        1.05,
        1.2,
        1.1,
        1.3,
        1.0,
        1.2,
        1.15,
        1.25,
        1.05,
    ]

    result = bootstrap_sharpe_ci(sharpe_ratios, n_bootstrap=1000, seed=42)

    assert "mean" in result
    assert "std" in result
    assert "lower" in result
    assert "upper" in result
    assert "confidence" in result

    assert result["lower"] < result["upper"]
    # CI should contain the sample mean
    assert result["lower"] <= result["mean"] <= result["upper"]
    # Std should be non-negative
    assert result["std"] >= 0.0


def test_bootstrap_sharpe_ci_confidence() -> None:
    """With confidence=0.99, CI should be wider than with confidence=0.90."""
    from quant_foundry.modules.benchmark.significance import bootstrap_sharpe_ci

    sharpe_ratios = [
        1.2,
        1.1,
        1.3,
        1.0,
        1.2,
        1.15,
        1.25,
        1.05,
        1.2,
        1.1,
        1.3,
        1.0,
        1.2,
        1.15,
        1.25,
        1.05,
    ]

    ci_90 = bootstrap_sharpe_ci(sharpe_ratios, n_bootstrap=2000, confidence=0.90, seed=42)
    ci_99 = bootstrap_sharpe_ci(sharpe_ratios, n_bootstrap=2000, confidence=0.99, seed=42)

    width_90 = ci_90["upper"] - ci_90["lower"]
    width_99 = ci_99["upper"] - ci_99["lower"]

    assert width_99 >= width_90, f"99% CI width {width_99} should be >= 90% CI width {width_90}"


def test_bootstrap_sharpe_ci_reproducible() -> None:
    """Same input + same seed should produce same output."""
    from quant_foundry.modules.benchmark.significance import bootstrap_sharpe_ci

    sharpe_ratios = [1.2, 1.1, 1.3, 1.0, 1.2, 1.15, 1.25, 1.05]

    result_1 = bootstrap_sharpe_ci(sharpe_ratios, n_bootstrap=500, seed=123)
    result_2 = bootstrap_sharpe_ci(sharpe_ratios, n_bootstrap=500, seed=123)

    assert result_1["lower"] == result_2["lower"]
    assert result_1["upper"] == result_2["upper"]
    assert result_1["mean"] == result_2["mean"]


# --------------------------------------------------------------------------- #
# Bootstrap Sharpe difference CI tests                                         #
# --------------------------------------------------------------------------- #


def test_bootstrap_difference_ci_significant() -> None:
    """When two lists are clearly different, significant_at_5pct should be True."""
    from quant_foundry.modules.benchmark.significance import (
        bootstrap_sharpe_difference_ci,
    )

    # Config A clearly better (higher Sharpe) than Config B
    sharpe_a = [
        2.0,
        2.1,
        1.9,
        2.05,
        2.15,
        1.95,
        2.0,
        2.1,
        2.0,
        2.1,
        1.9,
        2.05,
        2.15,
        1.95,
        2.0,
        2.1,
    ]
    sharpe_b = [
        0.5,
        0.6,
        0.4,
        0.55,
        0.65,
        0.45,
        0.5,
        0.6,
        0.5,
        0.6,
        0.4,
        0.55,
        0.65,
        0.45,
        0.5,
        0.6,
    ]

    result = bootstrap_sharpe_difference_ci(sharpe_a, sharpe_b, n_bootstrap=2000, seed=42)

    assert result["significant_at_5pct"] is True
    assert result["mean_diff"] > 0.0
    # CI should not contain 0
    assert result["lower"] > 0.0 or result["upper"] < 0.0


def test_bootstrap_difference_ci_not_significant() -> None:
    """When two lists overlap heavily, significant_at_5pct should be False."""
    from quant_foundry.modules.benchmark.significance import (
        bootstrap_sharpe_difference_ci,
    )

    # Two configs with nearly identical Sharpe distributions
    sharpe_a = [1.0, 1.1, 0.9, 1.05, 0.95, 1.0, 1.1, 0.9, 1.0, 1.1, 0.9, 1.05, 0.95, 1.0, 1.1, 0.9]
    sharpe_b = [
        1.0,
        1.05,
        0.95,
        1.0,
        1.1,
        0.9,
        1.05,
        0.95,
        1.0,
        1.05,
        0.95,
        1.0,
        1.1,
        0.9,
        1.05,
        0.95,
    ]

    result = bootstrap_sharpe_difference_ci(sharpe_a, sharpe_b, n_bootstrap=2000, seed=42)

    assert result["significant_at_5pct"] is False
    # CI should contain 0
    assert result["lower"] <= 0.0 <= result["upper"]


# --------------------------------------------------------------------------- #
# MultiSeedRunner tests                                                        #
# --------------------------------------------------------------------------- #


def test_multi_seed_runner() -> None:
    """MultiSeedRunner runs n_seeds and aggregates mean/std of Sharpe."""
    from quant_foundry.modules.benchmark.harness import (
        BenchmarkConfig,
        BenchmarkResult,
    )
    from quant_foundry.modules.benchmark.multi_seed import MultiSeedRunner

    config = BenchmarkConfig(
        name="test-config",
        universe="u",
        source="s",
        sentiment="se",
        features=["f"],
        label="l",
        price_join="p",
        start_ns=0,
        end_ns=1,
    )

    runner = MultiSeedRunner(config, n_seeds=3, output_dir=pathlib.Path("/tmp/bench"))

    # Mock BenchmarkHarness so no real training happens
    def make_mock_result(seed: int) -> BenchmarkResult:
        c = BenchmarkConfig(
            name=f"test-config_seed{seed}",
            universe="u",
            source="s",
            sentiment="se",
            features=["f"],
            label="l",
            price_join="p",
            start_ns=0,
            end_ns=1,
            random_seed=seed,
        )
        mock_dossier = MagicMock()
        mock_dossier.deflated_sharpe = 1.0 + seed * 0.1  # 1.0, 1.1, 1.2
        mock_dossier.pbo = 0.1 + seed * 0.01
        mock_dossier.to_json.return_value = "{}"
        return BenchmarkResult(config=c, dataset_id=f"test_seed{seed}", dossier=mock_dossier)

    mock_results = [make_mock_result(i) for i in range(3)]

    with patch("quant_foundry.modules.benchmark.multi_seed.BenchmarkHarness") as mock_harness_cls:
        mock_harness = MagicMock()
        mock_harness.run.return_value = mock_results
        mock_harness_cls.return_value = mock_harness

        result = runner.run()

    # Should have 3 results
    assert len(result.results) == 3
    # Sharpe values: [1.0, 1.1, 1.2]
    assert result.sharpe_values == [1.0, 1.1, 1.2]
    # Mean = 1.1
    assert abs(result.sharpe_mean - 1.1) < 1e-6
    # Std = std of [1.0, 1.1, 1.2] with ddof=1
    expected_std = ((0.01 + 0.0 + 0.01) / 2) ** 0.5  # = 0.1
    assert abs(result.sharpe_std - expected_std) < 1e-6
    # All succeeded
    assert result.all_succeeded is True


# --------------------------------------------------------------------------- #
# PlaceboTest tests                                                            #
# --------------------------------------------------------------------------- #


class _DummyModel:
    """A trivial model that learns the mean of y and predicts it.

    Used for placebo tests — simple enough to run without heavy deps.
    """

    def __init__(self) -> None:
        self._mean: float = 0.0

    def fit(self, X: list[list[float]], y: list[float]) -> _DummyModel:
        if y:
            self._mean = sum(y) / len(y)
        return self

    def predict(self, X: list[list[float]]) -> list[float]:
        return [self._mean for _ in X]


class _LinearDummyModel:
    """A simple model that learns a linear coefficient per feature.

    Predicts sum(coef_i * x_i).  This allows feature-permutation to
    have a measurable effect (shuffling a feature breaks its
    relationship with the label).
    """

    def __init__(self) -> None:
        self._coefs: list[float] = []

    def fit(self, X: list[list[float]], y: list[float]) -> _LinearDummyModel:
        n_features = len(X[0]) if X else 0
        # Simple OLS-ish: coef_j = cov(x_j, y) / var(x_j)
        self._coefs = []
        n = len(X)
        if n == 0:
            return self
        y_mean = sum(y) / n
        for j in range(n_features):
            x_col = [X[i][j] for i in range(n)]
            x_mean = sum(x_col) / n
            cov = sum((x_col[i] - x_mean) * (y[i] - y_mean) for i in range(n))
            var = sum((xi - x_mean) ** 2 for xi in x_col)
            if var == 0.0:
                self._coefs.append(0.0)
            else:
                self._coefs.append(cov / var)
        return self

    def predict(self, X: list[list[float]]) -> list[float]:
        results = []
        for row in X:
            pred = sum(c * v for c, v in zip(self._coefs, row))
            results.append(pred)
        return results


def test_placebo_test_basic() -> None:
    """PlaceboTest.run returns p_value in [0,1] and significant_at_5pct as bool."""
    from quant_foundry.modules.benchmark.placebo import PlaceboTest

    model = _DummyModel()
    placebo = PlaceboTest()

    # Synthetic data: y depends on X
    X_train = [[1.0], [2.0], [3.0], [4.0], [5.0], [1.5], [2.5], [3.5], [4.5], [5.5]]
    y_train = [1.0, 2.0, 3.0, 4.0, 5.0, 1.5, 2.5, 3.5, 4.5, 5.5]
    X_test = [[1.2], [2.8], [4.1]]
    y_test = [1.2, 2.8, 4.1]

    result = placebo.run(
        model=model,
        feature_names=["feat1"],
        X_train=X_train,
        y_train=y_train,
        X_test=X_test,
        y_test=y_test,
        n_permutations=20,
        seed=42,
    )

    assert "real_metric" in result
    assert "permuted_metrics" in result
    assert "p_value" in result
    assert "significant_at_5pct" in result

    assert 0.0 <= result["p_value"] <= 1.0
    assert isinstance(result["significant_at_5pct"], bool)
    assert len(result["permuted_metrics"]) == 20


def test_placebo_feature_permutation() -> None:
    """Feature permutation returns feature_importance dict with feature names."""
    from quant_foundry.modules.benchmark.placebo import PlaceboTest

    model = _LinearDummyModel()
    placebo = PlaceboTest()

    # Two features: feat1 strongly correlated with y, feat2 noise
    X_train = [
        [1.0, 10.0],
        [2.0, 20.0],
        [3.0, 15.0],
        [4.0, 25.0],
        [5.0, 30.0],
        [6.0, 10.0],
        [7.0, 20.0],
        [8.0, 15.0],
        [9.0, 25.0],
        [10.0, 30.0],
    ]
    y_train = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    X_test = [
        [1.5, 12.0],
        [3.5, 18.0],
        [5.5, 22.0],
        [7.5, 28.0],
        [9.5, 14.0],
    ]
    y_test = [1.5, 3.5, 5.5, 7.5, 9.5]

    result = placebo.run_feature_permutation(
        model=model,
        feature_names=["feat1", "feat2"],
        X_train=X_train,
        y_train=y_train,
        X_test=X_test,
        y_test=y_test,
        n_permutations=10,
        seed=42,
    )

    assert "feature_importance" in result
    importance = result["feature_importance"]
    assert "feat1" in importance
    assert "feat2" in importance
    # feat1 is strongly correlated → permuting it should cause a bigger drop
    assert importance["feat1"] >= importance["feat2"]


# --------------------------------------------------------------------------- #
# ComparisonReport.significance_test tests                                     #
# --------------------------------------------------------------------------- #


def test_comparison_report_significance_test() -> None:
    """ComparisonReport.significance_test works with two lists of BenchmarkResult."""
    from quant_foundry.modules.benchmark.comparison import ComparisonReport
    from quant_foundry.modules.benchmark.harness import (
        BenchmarkConfig,
        BenchmarkResult,
    )

    def make_result(name: str, dsr: float) -> BenchmarkResult:
        c = BenchmarkConfig(
            name=name,
            universe="u",
            source="source:newsapi:1.0.0",
            sentiment="sentiment:finbert:1.0.0",
            features=["f"],
            label="l",
            price_join="p",
            start_ns=0,
            end_ns=1,
        )
        mock_dossier = MagicMock()
        mock_dossier.deflated_sharpe = dsr
        mock_dossier.pbo = 0.1
        mock_dossier.metadata = {}
        mock_dossier.to_json.return_value = "{}"
        return BenchmarkResult(config=c, dataset_id=name, dossier=mock_dossier)

    # Config A: high Sharpe across seeds
    results_a = [
        make_result("a_seed0", 2.0),
        make_result("a_seed1", 2.1),
        make_result("a_seed2", 1.9),
        make_result("a_seed3", 2.05),
    ]
    # Config B: low Sharpe across seeds
    results_b = [
        make_result("b_seed0", 0.5),
        make_result("b_seed1", 0.6),
        make_result("b_seed2", 0.4),
        make_result("b_seed3", 0.55),
    ]

    comparison = ComparisonReport.from_results(results_a + results_b)
    sig = comparison.significance_test(results_a, results_b, n_bootstrap=1000)

    assert "mean_diff" in sig
    assert "significant_at_5pct" in sig
    assert sig["significant_at_5pct"] is True
    assert sig["mean_diff"] > 0.0  # A is better
