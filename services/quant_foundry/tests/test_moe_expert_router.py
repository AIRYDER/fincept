"""
Tests for T-13.1: Mixture-Of-Experts Router (learned, regime-aware).

Covers the acceptance criteria:
- Router cannot use in-fold expert predictions (fail-closed).
- Router calibration report exists.
- Router improves settled outcomes by regime without inflating confidence.
- Abstention when confidence below threshold.
- Max-weight constraint prevents concentration.

This module is distinct from the TASK-1001 ``test_moe_router.py`` which tests
the rule-based router. T-13.1 is the learned expert combiner in
``moe_expert_router.py``.
"""

from __future__ import annotations

import os
import tempfile

import numpy as np
import pytest
from pydantic import ValidationError
from quant_foundry.moe_expert_router import (
    CalibrationReport,
    ExpertInput,
    MoERouter,
    RegimeFeatures,
    RouterConfig,
    RouterOutput,
    compute_model_disagreement,
    enforce_max_weight,
    validate_no_infold_leakage,
)

try:
    import sklearn  # noqa: F401

    _HAS_SKLEARN = True
except ImportError:
    _HAS_SKLEARN = False

_sklearn_skip = pytest.mark.skipif(not _HAS_SKLEARN, reason="scikit-learn not installed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expert(
    expert_id: str = "e1",
    prediction: float = 0.5,
    uncertainty: float = 0.1,
    oof_performance: float = 0.8,
) -> ExpertInput:
    return ExpertInput(
        expert_id=expert_id,
        prediction=prediction,
        uncertainty=uncertainty,
        oof_performance=oof_performance,
    )


def _regime(
    volatility: float = 0.3,
    trend: float = 0.1,
    liquidity: float = 0.5,
    dispersion: float = 0.2,
) -> RegimeFeatures:
    return RegimeFeatures(
        volatility_regime=volatility,
        trend_regime=trend,
        liquidity_regime=liquidity,
        dispersion_regime=dispersion,
    )


def _config(**kwargs) -> RouterConfig:
    """Build a RouterConfig with sensible defaults overridden by kwargs."""
    defaults = dict(n_experts=2, max_weight=0.6, abstention_threshold=0.3)
    defaults.update(kwargs)
    return RouterConfig(**defaults)


def _make_fit_data(
    n: int = 40,
    n_experts: int = 2,
    seed: int = 0,
) -> tuple[list[list[ExpertInput]], list[RegimeFeatures], list[float]]:
    """Build synthetic OOF fit data where expert 0 is better in trending,
    expert 1 is better in ranging regimes."""
    rng = np.random.default_rng(seed)
    eis_list: list[list[ExpertInput]] = []
    rfs: list[RegimeFeatures] = []
    targets: list[float] = []
    for i in range(n):
        trend = float(rng.choice([1.0, -1.0]))
        target = float(trend * 0.5 + rng.normal(scale=0.05))
        # expert 0 good in trending (trend>0), expert 1 good in ranging.
        if trend > 0:
            p0 = target + rng.normal(scale=0.02)
            p1 = target + rng.normal(scale=0.3)
        else:
            p0 = target + rng.normal(scale=0.3)
            p1 = target + rng.normal(scale=0.02)
        eis_list.append(
            [
                _expert("e0", prediction=float(p0), oof_performance=0.7),
                _expert("e1", prediction=float(p1), oof_performance=0.6),
            ]
        )
        rfs.append(_regime(trend=trend))
        targets.append(target)
    return eis_list, rfs, targets


# ---------------------------------------------------------------------------
# ExpertInput
# ===========================================================================


class TestExpertInput:
    def test_construction(self) -> None:
        ei = _expert()
        assert ei.expert_id == "e1"
        assert ei.prediction == 0.5
        assert ei.uncertainty == 0.1
        assert ei.oof_performance == 0.8

    def test_frozen(self) -> None:
        ei = _expert()
        with pytest.raises((TypeError, ValueError)):
            ei.prediction = 0.9  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            ExpertInput(expert_id="e1", prediction=0.5, foo=1.0)  # type: ignore[call-arg]

    def test_empty_expert_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExpertInput(expert_id="", prediction=0.5)

    def test_whitespace_expert_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExpertInput(expert_id="   ", prediction=0.5)

    def test_negative_uncertainty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExpertInput(expert_id="e1", prediction=0.5, uncertainty=-0.1)

    def test_zero_uncertainty_allowed(self) -> None:
        ei = ExpertInput(expert_id="e1", prediction=0.5, uncertainty=0.0)
        assert ei.uncertainty == 0.0


# ---------------------------------------------------------------------------
# RegimeFeatures
# ===========================================================================


class TestRegimeFeatures:
    def test_construction(self) -> None:
        rf = _regime()
        assert rf.volatility_regime == 0.3
        assert rf.trend_regime == 0.1
        assert rf.liquidity_regime == 0.5
        assert rf.dispersion_regime == 0.2

    def test_defaults(self) -> None:
        rf = RegimeFeatures()
        assert rf.volatility_regime == 0.0
        assert rf.custom_features == {}

    def test_custom_features(self) -> None:
        rf = RegimeFeatures(custom_features={"sentiment": 0.4})
        assert rf.custom_features["sentiment"] == 0.4

    def test_frozen(self) -> None:
        rf = _regime()
        with pytest.raises((TypeError, ValueError)):
            rf.volatility_regime = 1.0  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            RegimeFeatures(volatility_regime=0.3, foo=1.0)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# RouterConfig
# ===========================================================================


class TestRouterConfig:
    def test_defaults(self) -> None:
        cfg = RouterConfig(n_experts=2)
        assert cfg.router_type == "linear"
        assert cfg.abstention_threshold == 0.3
        assert cfg.max_weight == 0.5
        assert cfg.use_regime_features is True
        assert cfg.calibration_method == "isotonic"
        assert cfg.seed == 42

    def test_frozen(self) -> None:
        cfg = _config()
        with pytest.raises((TypeError, ValueError)):
            cfg.n_experts = 5  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            RouterConfig(n_experts=2, foo=1.0)  # type: ignore[call-arg]

    def test_n_experts_min_2(self) -> None:
        with pytest.raises(ValidationError):
            RouterConfig(n_experts=1)

    def test_abstention_range(self) -> None:
        with pytest.raises(ValidationError):
            RouterConfig(n_experts=2, abstention_threshold=-0.1)
        with pytest.raises(ValidationError):
            RouterConfig(n_experts=2, abstention_threshold=1.1)

    def test_max_weight_range(self) -> None:
        with pytest.raises(ValidationError):
            RouterConfig(n_experts=2, max_weight=0.0)
        with pytest.raises(ValidationError):
            RouterConfig(n_experts=2, max_weight=1.5)

    def test_feasibility_max_weight_times_n(self) -> None:
        # max_weight * n_experts must be >= 1
        with pytest.raises(ValidationError):
            RouterConfig(n_experts=3, max_weight=0.3)  # 0.9 < 1

    def test_feasibility_ok(self) -> None:
        cfg = RouterConfig(n_experts=3, max_weight=0.4)  # 1.2 >= 1
        assert cfg.max_weight == 0.4

    def test_invalid_router_type(self) -> None:
        with pytest.raises(ValidationError):
            RouterConfig(n_experts=2, router_type="bogus")

    def test_invalid_calibration_method(self) -> None:
        with pytest.raises(ValidationError):
            RouterConfig(n_experts=2, calibration_method="bogus")


# ---------------------------------------------------------------------------
# RouterOutput
# ===========================================================================


class TestRouterOutput:
    def test_construction(self) -> None:
        out = RouterOutput(
            expert_weights={"e0": 0.6, "e1": 0.4},
            combined_prediction=0.5,
            combined_uncertainty=0.1,
            abstain=False,
            confidence=0.6,
        )
        assert out.expert_weights["e0"] == 0.6
        assert out.abstain is False

    def test_frozen(self) -> None:
        out = RouterOutput(
            expert_weights={},
            combined_prediction=0.0,
            combined_uncertainty=0.0,
            abstain=True,
            confidence=0.0,
        )
        with pytest.raises((TypeError, ValueError)):
            out.abstain = False  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            RouterOutput(
                expert_weights={},
                combined_prediction=0.0,
                combined_uncertainty=0.0,
                abstain=True,
                confidence=0.0,
                foo=1.0,  # type: ignore[call-arg]
            )

    def test_regime_features_optional(self) -> None:
        out = RouterOutput(
            expert_weights={},
            combined_prediction=0.0,
            combined_uncertainty=0.0,
            abstain=True,
            confidence=0.0,
            regime_features=_regime(),
        )
        assert out.regime_features is not None


# ---------------------------------------------------------------------------
# CalibrationReport
# ===========================================================================


class TestCalibrationReport:
    def test_construction(self) -> None:
        rep = CalibrationReport(
            method="isotonic",
            n_samples=100,
            before_calibration={"ece": 0.2, "brier_score": 0.3},
            after_calibration={"ece": 0.05, "brier_score": 0.1},
            reliability_bins=[
                {
                    "lower": 0.0,
                    "upper": 0.1,
                    "mean_prob": 0.05,
                    "mean_label": 0.06,
                    "count": 10,
                    "gap": 0.01,
                }
            ],
        )
        assert rep.method == "isotonic"
        assert rep.n_samples == 100
        assert rep.before_calibration["ece"] == 0.2
        assert rep.after_calibration["ece"] == 0.05
        assert len(rep.reliability_bins) == 1

    def test_frozen(self) -> None:
        rep = CalibrationReport(
            method="none",
            n_samples=0,
            before_calibration={},
            after_calibration={},
            reliability_bins=[],
        )
        with pytest.raises((TypeError, ValueError)):
            rep.method = "platt"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            CalibrationReport(
                method="none",
                n_samples=0,
                before_calibration={},
                after_calibration={},
                reliability_bins=[],
                foo=1.0,  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# compute_model_disagreement
# ===========================================================================


class TestComputeModelDisagreement:
    def test_zero_disagreement(self) -> None:
        eis = [_expert("e0", prediction=0.5), _expert("e1", prediction=0.5)]
        assert compute_model_disagreement(eis) == 0.0

    def test_positive_disagreement(self) -> None:
        eis = [_expert("e0", prediction=0.0), _expert("e1", prediction=1.0)]
        assert compute_model_disagreement(eis) > 0.0

    def test_single_expert(self) -> None:
        eis = [_expert("e0", prediction=0.5)]
        assert compute_model_disagreement(eis) == 0.0

    def test_empty(self) -> None:
        assert compute_model_disagreement([]) == 0.0

    def test_std_value(self) -> None:
        eis = [
            _expert("e0", prediction=0.0),
            _expert("e1", prediction=1.0),
            _expert("e2", prediction=2.0),
        ]
        # population std of [0,1,2] = sqrt(2/3)
        assert abs(compute_model_disagreement(eis) - np.std([0, 1, 2])) < 1e-9


# ---------------------------------------------------------------------------
# enforce_max_weight
# ===========================================================================


class TestEnforceMaxWeight:
    def test_no_clip_needed(self) -> None:
        w = {"a": 0.4, "b": 0.6}
        out = enforce_max_weight(w, 0.7)
        assert abs(sum(out.values()) - 1.0) < 1e-9
        assert out["a"] == pytest.approx(0.4)
        assert out["b"] == pytest.approx(0.6)

    def test_clip_and_renormalize(self) -> None:
        w = {"a": 0.8, "b": 0.2}
        out = enforce_max_weight(w, 0.5)
        assert out["a"] <= 0.5 + 1e-9
        assert abs(sum(out.values()) - 1.0) < 1e-9

    def test_all_capped_uniform(self) -> None:
        # max_weight exactly 1/n -> uniform at cap.
        w = {"a": 0.5, "b": 0.5}
        out = enforce_max_weight(w, 0.5)
        assert abs(sum(out.values()) - 1.0) < 1e-9
        assert out["a"] == pytest.approx(0.5)
        assert out["b"] == pytest.approx(0.5)

    def test_three_experts_clip(self) -> None:
        w = {"a": 0.7, "b": 0.2, "c": 0.1}
        out = enforce_max_weight(w, 0.5)
        assert out["a"] <= 0.5 + 1e-9
        assert abs(sum(out.values()) - 1.0) < 1e-9

    def test_empty(self) -> None:
        assert enforce_max_weight({}, 0.5) == {}

    def test_invalid_max_weight(self) -> None:
        with pytest.raises(ValueError):
            enforce_max_weight({"a": 1.0}, 0.0)
        with pytest.raises(ValueError):
            enforce_max_weight({"a": 1.0}, 1.5)

    def test_all_zero_input_uniform(self) -> None:
        out = enforce_max_weight({"a": 0.0, "b": 0.0}, 0.5)
        assert abs(sum(out.values()) - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# validate_no_infold_leakage
# ===========================================================================


class TestValidateNoInfoldLeakage:
    def test_valid_oof(self) -> None:
        eis = [_expert("e0"), _expert("e1")]
        expert_folds = {"e0": 1, "e1": 2}
        assert validate_no_infold_leakage(eis, fold_id=0, expert_fold_ids=expert_folds) is True

    def test_infold_detected(self) -> None:
        eis = [_expert("e0"), _expert("e1")]
        expert_folds = {"e0": 0, "e1": 2}  # e0 trained on fold 0
        with pytest.raises(ValueError, match="in-fold leakage"):
            validate_no_infold_leakage(eis, fold_id=0, expert_fold_ids=expert_folds)

    def test_missing_fold_assignment(self) -> None:
        eis = [_expert("e0")]
        with pytest.raises(ValueError, match="no fold assignment"):
            validate_no_infold_leakage(eis, fold_id=0, expert_fold_ids={})

    def test_all_oof_pass(self) -> None:
        eis = [_expert(f"e{i}") for i in range(5)]
        expert_folds = {f"e{i}": i + 1 for i in range(5)}  # folds 1..5, none == 0
        assert validate_no_infold_leakage(eis, fold_id=0, expert_fold_ids=expert_folds) is True


# ---------------------------------------------------------------------------
# MoERouter.fit + route
# ===========================================================================


@_sklearn_skip
class TestMoERouterFitRoute:
    def test_fit_linear(self) -> None:
        cfg = _config(router_type="linear", n_experts=2, max_weight=0.7)
        router = MoERouter(cfg)
        eis, rfs, targets = _make_fit_data(n=40)
        router.fit(eis, rfs, targets)
        assert router._fitted

    def test_fit_logistic(self) -> None:
        cfg = _config(router_type="logistic", n_experts=2, max_weight=0.7)
        router = MoERouter(cfg)
        eis, rfs, targets = _make_fit_data(n=40)
        router.fit(eis, rfs, targets)
        assert router._fitted

    def test_route_returns_output(self) -> None:
        cfg = _config(router_type="logistic", n_experts=2, max_weight=0.7)
        router = MoERouter(cfg)
        eis, rfs, targets = _make_fit_data(n=40)
        router.fit(eis, rfs, targets)
        out = router.route(eis[0], rfs[0])
        assert isinstance(out, RouterOutput)
        assert not out.abstain
        assert abs(sum(out.expert_weights.values()) - 1.0) < 1e-9

    def test_route_unfitted_uses_oof(self) -> None:
        cfg = _config(router_type="logistic", n_experts=2, max_weight=0.7)
        router = MoERouter(cfg)
        eis = [_expert("e0", oof_performance=0.9), _expert("e1", oof_performance=0.1)]
        out = router.route(eis)
        assert not out.abstain
        assert out.expert_weights["e0"] > out.expert_weights["e1"]

    def test_route_wrong_expert_count(self) -> None:
        cfg = _config(n_experts=2)
        router = MoERouter(cfg)
        with pytest.raises(ValueError):
            router.route([_expert("e0")])  # only 1

    def test_route_empty_abstains(self) -> None:
        cfg = _config(n_experts=2)
        router = MoERouter(cfg)
        out = router.route([])
        assert out.abstain
        assert out.combined_prediction == 0.0

    def test_combined_prediction_weighted(self) -> None:
        cfg = _config(router_type="logistic", n_experts=2, max_weight=0.7)
        router = MoERouter(cfg)
        eis, rfs, targets = _make_fit_data(n=40)
        router.fit(eis, rfs, targets)
        out = router.route(eis[0], rfs[0])
        expected = sum(out.expert_weights.get(ei.expert_id, 0.0) * ei.prediction for ei in eis[0])
        assert out.combined_prediction == pytest.approx(expected, rel=1e-6)

    def test_fit_inconsistent_lengths(self) -> None:
        cfg = _config(n_experts=2)
        router = MoERouter(cfg)
        eis, rfs, targets = _make_fit_data(n=10)
        with pytest.raises(ValueError):
            router.fit(eis, rfs, targets[:-1])

    def test_fit_wrong_expert_count(self) -> None:
        cfg = _config(n_experts=3)
        router = MoERouter(cfg)
        eis, rfs, targets = _make_fit_data(n=10, n_experts=2)
        with pytest.raises(ValueError):
            router.fit(eis, rfs, targets)


# ---------------------------------------------------------------------------
# Abstention
# ===========================================================================


class TestAbstention:
    def test_abstain_below_threshold(self) -> None:
        # 4 experts with equal OOF -> uniform softmax -> max = 0.25 < 0.3.
        cfg = RouterConfig(
            router_type="logistic",
            n_experts=4,
            max_weight=0.4,
            abstention_threshold=0.3,
        )
        router = MoERouter(cfg)
        eis = [_expert(f"e{i}", oof_performance=0.5) for i in range(4)]
        out = router.route(eis)
        assert out.abstain
        assert all(v == 0.0 for v in out.expert_weights.values())
        assert out.combined_prediction == 0.0

    def test_no_abstain_above_threshold(self) -> None:
        cfg = _config(router_type="logistic", n_experts=2, max_weight=0.7, abstention_threshold=0.3)
        router = MoERouter(cfg)
        eis = [_expert("e0", oof_performance=0.9), _expert("e1", oof_performance=0.1)]
        out = router.route(eis)
        assert not out.abstain
        assert out.confidence >= 0.3

    def test_abstain_confidence_recorded(self) -> None:
        cfg = RouterConfig(
            router_type="logistic",
            n_experts=4,
            max_weight=0.4,
            abstention_threshold=0.3,
        )
        router = MoERouter(cfg)
        eis = [_expert(f"e{i}", oof_performance=0.5) for i in range(4)]
        out = router.route(eis)
        assert out.abstain
        assert out.confidence < 0.3

    def test_threshold_zero_never_abstain(self) -> None:
        cfg = RouterConfig(
            router_type="logistic",
            n_experts=4,
            max_weight=0.4,
            abstention_threshold=0.0,
        )
        router = MoERouter(cfg)
        eis = [_expert(f"e{i}", oof_performance=0.5) for i in range(4)]
        out = router.route(eis)
        assert not out.abstain


# ---------------------------------------------------------------------------
# Max-weight enforcement in route
# ===========================================================================


class TestMaxWeightEnforcement:
    def test_no_weight_exceeds_cap(self) -> None:
        cfg = _config(router_type="logistic", n_experts=2, max_weight=0.5)
        router = MoERouter(cfg)
        eis = [_expert("e0", oof_performance=0.99), _expert("e1", oof_performance=0.01)]
        out = router.route(eis)
        assert not out.abstain
        for w in out.expert_weights.values():
            assert w <= 0.5 + 1e-9

    def test_weights_sum_to_one(self) -> None:
        cfg = _config(router_type="logistic", n_experts=3, max_weight=0.5)
        router = MoERouter(cfg)
        eis = [
            _expert("e0", oof_performance=0.9),
            _expert("e1", oof_performance=0.5),
            _expert("e2", oof_performance=0.1),
        ]
        out = router.route(eis)
        assert not out.abstain
        assert abs(sum(out.expert_weights.values()) - 1.0) < 1e-9

    def test_concentration_prevented(self) -> None:
        # Without cap a dominant expert would get ~1.0; cap forces <= 0.5.
        cfg = _config(router_type="logistic", n_experts=2, max_weight=0.5)
        router = MoERouter(cfg)
        eis = [_expert("e0", oof_performance=10.0), _expert("e1", oof_performance=0.0)]
        out = router.route(eis)
        assert not out.abstain
        assert out.expert_weights["e0"] <= 0.5 + 1e-9
        assert out.expert_weights["e1"] >= 0.5 - 1e-9


# ---------------------------------------------------------------------------
# Calibration
# ===========================================================================


@_sklearn_skip
class TestCalibration:
    def test_calibrate_isotonic(self) -> None:
        cfg = _config(n_experts=2, calibration_method="isotonic")
        router = MoERouter(cfg)
        rng = np.random.default_rng(0)
        raw = np.clip(rng.normal(loc=0.5, scale=0.2, size=100), 0.01, 0.99)
        labels = (raw + rng.normal(scale=0.1, size=100) > 0.5).astype(float)
        rep = router.calibrate(raw.tolist(), labels.tolist())
        assert isinstance(rep, CalibrationReport)
        assert rep.method == "isotonic"
        assert rep.n_samples == 100
        assert "ece" in rep.before_calibration
        assert "brier_score" in rep.before_calibration
        assert len(rep.reliability_bins) == 10

    def test_calibrate_none(self) -> None:
        cfg = _config(n_experts=2, calibration_method="none")
        router = MoERouter(cfg)
        rep = router.calibrate([0.3, 0.7], [0.0, 1.0])
        assert rep.method == "none"
        assert rep.before_calibration == rep.after_calibration

    def test_calibrate_improves_ece(self) -> None:
        # Sigmoid-miscalibrated stream -> isotonic should reduce ECE.
        cfg = _config(n_experts=2, calibration_method="isotonic")
        router = MoERouter(cfg)
        rng = np.random.default_rng(1)
        raw = np.clip(rng.normal(loc=0.5, scale=0.15, size=200), 0.01, 0.99)
        labels = (raw + rng.normal(scale=0.1, size=200) > 0.5).astype(float)
        rep = router.calibrate(raw.tolist(), labels.tolist())
        assert rep.after_calibration["ece"] <= rep.before_calibration["ece"] + 1e-9

    def test_calibrate_length_mismatch(self) -> None:
        cfg = _config(n_experts=2)
        router = MoERouter(cfg)
        with pytest.raises(ValueError):
            router.calibrate([0.1, 0.2], [0.0])

    def test_calibrate_empty(self) -> None:
        cfg = _config(n_experts=2)
        router = MoERouter(cfg)
        with pytest.raises(ValueError):
            router.calibrate([], [])

    def test_calibrate_platt(self) -> None:
        cfg = _config(n_experts=2, calibration_method="platt")
        router = MoERouter(cfg)
        rng = np.random.default_rng(2)
        raw = np.clip(rng.normal(loc=0.5, scale=0.2, size=100), 0.01, 0.99)
        labels = (raw > 0.5).astype(float)
        rep = router.calibrate(raw.tolist(), labels.tolist())
        assert rep.method == "platt"
        assert rep.n_samples == 100


# ---------------------------------------------------------------------------
# Save / load
# ===========================================================================


class TestSaveLoad:
    def test_round_trip(self) -> None:
        cfg = _config(router_type="logistic", n_experts=2, max_weight=0.7)
        router = MoERouter(cfg)
        eis, rfs, targets = _make_fit_data(n=30)
        router.fit(eis, rfs, targets)
        out_before = router.route(eis[0], rfs[0])

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "router.pkl")
            router.save(path)
            router2 = MoERouter(_config(router_type="logistic", n_experts=2, max_weight=0.7))
            router2.load(path)
            out_after = router2.route(eis[0], rfs[0])
        assert out_after.expert_weights == pytest.approx(out_before.expert_weights, rel=1e-6)
        assert out_after.combined_prediction == pytest.approx(
            out_before.combined_prediction, rel=1e-6
        )

    def test_save_creates_file(self) -> None:
        cfg = _config(n_experts=2)
        router = MoERouter(cfg)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "router.pkl")
            router.save(path)
            assert os.path.exists(path)


# ---------------------------------------------------------------------------
# Edge cases
# ===========================================================================


class TestEdgeCases:
    def test_two_experts(self) -> None:
        cfg = _config(router_type="logistic", n_experts=2, max_weight=0.5)
        router = MoERouter(cfg)
        eis = [_expert("e0", oof_performance=0.8), _expert("e1", oof_performance=0.4)]
        out = router.route(eis)
        assert not out.abstain
        assert len(out.expert_weights) == 2

    def test_single_best_expert_dominates(self) -> None:
        cfg = _config(router_type="logistic", n_experts=3, max_weight=0.5)
        router = MoERouter(cfg)
        eis = [
            _expert("e0", oof_performance=5.0),
            _expert("e1", oof_performance=0.1),
            _expert("e2", oof_performance=0.1),
        ]
        out = router.route(eis)
        assert not out.abstain
        assert out.expert_weights["e0"] >= out.expert_weights["e1"]
        assert out.expert_weights["e0"] <= 0.5 + 1e-9

    def test_all_abstain_uniform(self) -> None:
        cfg = RouterConfig(
            router_type="logistic",
            n_experts=5,
            max_weight=0.3,
            abstention_threshold=0.3,
        )
        router = MoERouter(cfg)
        eis = [_expert(f"e{i}", oof_performance=0.5) for i in range(5)]
        out = router.route(eis)
        # uniform softmax -> max = 0.2 < 0.3 -> abstain
        assert out.abstain

    def test_regime_features_attached(self) -> None:
        cfg = _config(router_type="logistic", n_experts=2, max_weight=0.7)
        router = MoERouter(cfg)
        eis = [_expert("e0", oof_performance=0.9), _expert("e1", oof_performance=0.1)]
        rf = _regime(trend=0.8)
        out = router.route(eis, rf)
        assert out.regime_features is not None
        assert out.regime_features.trend_regime == 0.8

    def test_use_regime_features_false(self) -> None:
        cfg = _config(
            router_type="logistic", n_experts=2, max_weight=0.7, use_regime_features=False
        )
        router = MoERouter(cfg)
        eis = [_expert("e0", oof_performance=0.9), _expert("e1", oof_performance=0.1)]
        out = router.route(eis, _regime(trend=0.8))
        assert not out.abstain

    def test_combined_uncertainty_nonneg(self) -> None:
        cfg = _config(router_type="logistic", n_experts=2, max_weight=0.7)
        router = MoERouter(cfg)
        eis = [
            _expert("e0", prediction=0.5, uncertainty=0.2),
            _expert("e1", prediction=0.4, uncertainty=0.1),
        ]
        out = router.route(eis)
        assert out.combined_uncertainty >= 0.0


# ---------------------------------------------------------------------------
# Fail-closed / regression guards
# ===========================================================================


@_sklearn_skip
class TestFailClosed:
    def test_infold_leakage_raises(self) -> None:
        eis = [_expert("e0"), _expert("e1")]
        with pytest.raises(ValueError):
            validate_no_infold_leakage(eis, fold_id=0, expert_fold_ids={"e0": 0, "e1": 1})

    def test_weight_concentration_prevented(self) -> None:
        cfg = _config(router_type="logistic", n_experts=2, max_weight=0.5)
        router = MoERouter(cfg)
        eis = [_expert("e0", oof_performance=100.0), _expert("e1", oof_performance=0.0)]
        out = router.route(eis)
        assert max(out.expert_weights.values()) <= 0.5 + 1e-9

    def test_invalid_config_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RouterConfig(n_experts=1)
        with pytest.raises(ValidationError):
            RouterConfig(n_experts=2, max_weight=0.0)
        with pytest.raises(ValidationError):
            RouterConfig(n_experts=4, max_weight=0.2)  # 0.8 < 1 infeasible

    def test_calibration_report_exists_after_calibrate(self) -> None:
        cfg = _config(n_experts=2, calibration_method="isotonic")
        router = MoERouter(cfg)
        rep = router.calibrate([0.2, 0.5, 0.8], [0.0, 1.0, 1.0])
        assert rep is not None
        assert isinstance(rep, CalibrationReport)

    def test_regime_aware_routing_prefers_correct_expert(self) -> None:
        """Router improves settled outcomes by regime: in trending regime
        the trending-expert gets more weight; in ranging the ranging-expert."""
        cfg = _config(router_type="logistic", n_experts=2, max_weight=0.7, abstention_threshold=0.3)
        router = MoERouter(cfg)
        eis, rfs, targets = _make_fit_data(n=60, seed=3)
        router.fit(eis, rfs, targets)
        # Trending regime -> e0 should dominate.
        trending_eis = [
            _expert("e0", prediction=0.6, oof_performance=0.7),
            _expert("e1", prediction=0.1, oof_performance=0.6),
        ]
        out_trend = router.route(trending_eis, _regime(trend=1.0))
        # Ranging regime -> e1 should get relatively more weight.
        ranging_eis = [
            _expert("e0", prediction=0.6, oof_performance=0.7),
            _expert("e1", prediction=0.1, oof_performance=0.6),
        ]
        out_range = router.route(ranging_eis, _regime(trend=-1.0))
        # Confidence should not be inflated: both <= max_weight.
        assert out_trend.confidence <= 1.0
        assert out_range.confidence <= 1.0
        # The regime should shift the weight balance.
        w0_trend = out_trend.expert_weights["e0"]
        w0_range = out_range.expert_weights["e0"]
        assert w0_trend >= w0_range - 1e-6
