"""
Tests for T-7.4: Calibrated Probability Layer.

Covers the acceptance criteria from the spec:
- Calibration artifact present for classification.
- Missing calibration marks promotion ineligible if policy requires it.
- ECE, Brier, logloss, reliability buckets all emitted.
- Platt and isotonic both supported.
- Calibration improves ECE on synthetic miscalibrated data (calibrated < raw).
- Fail-closed: NONE method returns raw probs.
- Edge cases: empty probs, single bin, all-same probs.
- Artifact save / load round-trip.
- CalibrationPolicy eligibility checks.

File-disjoint from real_trainer.py — does not modify it.
"""

from __future__ import annotations

import pickle
from pathlib import Path

import pytest

from quant_foundry.calibration import (
    CalibrationMethod,
    CalibrationPolicy,
    CalibrationResult,
    Calibrator,
    ReliabilityBucket,
    calibrate,
    check_calibration_eligibility,
    compute_brier_score,
    compute_ece,
    compute_logloss,
    compute_reliability_buckets,
)

try:
    import sklearn  # noqa: F401
    _HAS_SKLEARN = True
except ImportError:
    _HAS_SKLEARN = False

_sklearn_skip = pytest.mark.skipif(not _HAS_SKLEARN, reason="scikit-learn not installed")


# ---------------------------------------------------------------------------
# Helpers
# ===========================================================================


def _sigmoid_miscalibrate(raw: list[float], steepness: float = 4.0) -> list[float]:
    """Apply a steep sigmoid distortion to simulate an overconfident model."""
    import math

    out: list[float] = []
    for p in raw:
        z = steepness * (p - 0.5)
        s = 1.0 / (1.0 + math.exp(-z))
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# CalibrationMethod enum
# ===========================================================================


class TestCalibrationMethod:
    def test_enum_members(self) -> None:
        assert CalibrationMethod.PLATT.value == "platt"
        assert CalibrationMethod.ISOTONIC.value == "isotonic"
        assert CalibrationMethod.NONE.value == "none"

    def test_enum_from_value(self) -> None:
        assert CalibrationMethod("platt") is CalibrationMethod.PLATT
        assert CalibrationMethod("isotonic") is CalibrationMethod.ISOTONIC
        assert CalibrationMethod("none") is CalibrationMethod.NONE


# ---------------------------------------------------------------------------
# CalibrationPolicy enum
# ===========================================================================


class TestCalibrationPolicy:
    def test_policy_members(self) -> None:
        assert CalibrationPolicy.REQUIRED.value == "required"
        assert CalibrationPolicy.OPTIONAL.value == "optional"
        assert CalibrationPolicy.NONE.value == "none"

    def test_policy_from_value(self) -> None:
        assert CalibrationPolicy("required") is CalibrationPolicy.REQUIRED


# ---------------------------------------------------------------------------
# ReliabilityBucket
# ===========================================================================


class TestReliabilityBucket:
    def test_bucket_fields(self) -> None:
        b = ReliabilityBucket(
            lower=0.0,
            upper=0.1,
            mean_prob=0.05,
            mean_label=0.0,
            count=3,
            gap=0.05,
        )
        assert b.lower == 0.0
        assert b.upper == 0.1
        assert b.mean_prob == pytest.approx(0.05)
        assert b.mean_label == 0.0
        assert b.count == 3
        assert b.gap == pytest.approx(0.05)

    def test_bucket_is_frozen(self) -> None:
        b = ReliabilityBucket(
            lower=0.0, upper=0.1, mean_prob=0.05, mean_label=0.0, count=3, gap=0.05
        )
        with pytest.raises((TypeError, ValueError)):
            b.count = 5  # type: ignore[misc]

    def test_bucket_extra_forbidden(self) -> None:
        with pytest.raises(Exception):
            ReliabilityBucket(
                lower=0.0,
                upper=0.1,
                mean_prob=0.05,
                mean_label=0.0,
                count=3,
                gap=0.05,
                extra_field=1,  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# CalibrationResult
# ===========================================================================


class TestCalibrationResult:
    def _make(self) -> CalibrationResult:
        return CalibrationResult(
            method=CalibrationMethod.PLATT,
            calibrated_probs=[0.2, 0.8],
            calibration_artifact_path="/tmp/cal.pkl",
            ece=0.01,
            brier_score=0.05,
            logloss=0.3,
            reliability_buckets=[
                ReliabilityBucket(
                    lower=0.0, upper=0.5, mean_prob=0.2, mean_label=0.0, count=1, gap=0.2
                )
            ],
        )

    def test_result_fields(self) -> None:
        r = self._make()
        assert r.method is CalibrationMethod.PLATT
        assert r.calibrated_probs == [0.2, 0.8]
        assert r.calibration_artifact_path == "/tmp/cal.pkl"
        assert r.ece == pytest.approx(0.01)
        assert r.brier_score == pytest.approx(0.05)
        assert r.logloss == pytest.approx(0.3)
        assert len(r.reliability_buckets) == 1

    def test_result_is_frozen(self) -> None:
        r = self._make()
        with pytest.raises((TypeError, ValueError)):
            r.ece = 0.5  # type: ignore[misc]

    def test_result_extra_forbidden(self) -> None:
        with pytest.raises(Exception):
            CalibrationResult(
                method=CalibrationMethod.NONE,
                calibrated_probs=[],
                calibration_artifact_path=None,
                ece=0.0,
                brier_score=0.0,
                logloss=0.0,
                reliability_buckets=[],
                extra=1,  # type: ignore[call-arg]
            )

    def test_result_artifact_path_nullable(self) -> None:
        r = CalibrationResult(
            method=CalibrationMethod.NONE,
            calibrated_probs=[],
            calibration_artifact_path=None,
            ece=0.0,
            brier_score=0.0,
            logloss=0.0,
            reliability_buckets=[],
        )
        assert r.calibration_artifact_path is None


# ---------------------------------------------------------------------------
# compute_ece
# ===========================================================================


class TestComputeECE:
    def test_perfect_calibration_ece_zero(self) -> None:
        probs = [0.0, 0.0, 1.0, 1.0]
        labels = [0, 0, 1, 1]
        assert compute_ece(probs, labels, n_bins=10) == pytest.approx(0.0)

    def test_known_ece_value(self) -> None:
        # Two bins (n_bins=2): bin0 [0,0.5), bin1 [0.5,1].
        # probs = [0.2, 0.4, 0.6, 0.8], labels = [0, 1, 0, 1]
        # bin0: probs mean=0.3, label mean=0.5, count=2 -> gap 0.2 * 0.5
        # bin1: probs mean=0.7, label mean=0.5, count=2 -> gap 0.2 * 0.5
        # ece = 0.1 + 0.1 = 0.2
        probs = [0.2, 0.4, 0.6, 0.8]
        labels = [0, 1, 0, 1]
        assert compute_ece(probs, labels, n_bins=2) == pytest.approx(0.2)

    def test_empty_returns_zero(self) -> None:
        assert compute_ece([], [], n_bins=10) == 0.0

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_ece([0.1, 0.2], [0], n_bins=10)

    def test_invalid_n_bins_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_ece([0.1], [0], n_bins=0)

    def test_single_bin(self) -> None:
        # n_bins=1: one bin [0,1]. probs mean=0.5, label mean=0.5 -> gap 0.
        probs = [0.2, 0.4, 0.6, 0.8]
        labels = [0, 0, 1, 1]
        assert compute_ece(probs, labels, n_bins=1) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# compute_brier_score
# ===========================================================================


class TestComputeBrierScore:
    def test_known_brier(self) -> None:
        # probs=[0.0,1.0], labels=[0,1] -> (0+0)/2 = 0
        assert compute_brier_score([0.0, 1.0], [0, 1]) == pytest.approx(0.0)

    def test_known_brier_nonzero(self) -> None:
        # probs=[0.5,0.5], labels=[0,1] -> (0.25+0.25)/2 = 0.25
        assert compute_brier_score([0.5, 0.5], [0, 1]) == pytest.approx(0.25)

    def test_empty_returns_zero(self) -> None:
        assert compute_brier_score([], []) == 0.0

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_brier_score([0.1, 0.2], [0])


# ---------------------------------------------------------------------------
# compute_logloss
# ===========================================================================


class TestComputeLogloss:
    def test_known_logloss(self) -> None:
        import math

        # probs=[0.5,0.5], labels=[0,1]
        # ll = -(0*log(0.5) + 1*log(0.5) + 1*log(0.5) + 0*log(0.5))/2
        #    = -log(0.5) = ln(2)
        ll = compute_logloss([0.5, 0.5], [0, 1])
        assert ll == pytest.approx(math.log(2.0))

    def test_perfect_logloss_zero(self) -> None:
        # Clamped probabilities -> log near 0.
        assert compute_logloss([1.0, 0.0], [1, 0]) == pytest.approx(0.0, abs=1e-6)

    def test_empty_returns_zero(self) -> None:
        assert compute_logloss([], []) == 0.0

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_logloss([0.1, 0.2], [0])

    def test_extreme_probs_clamped(self) -> None:
        import math

        # 0.0 and 1.0 must not produce -inf; should be finite.
        ll = compute_logloss([0.0, 1.0], [1, 0])
        assert math.isfinite(ll)


# ---------------------------------------------------------------------------
# compute_reliability_buckets
# ===========================================================================


class TestComputeReliabilityBuckets:
    def test_returns_n_bins(self) -> None:
        buckets = compute_reliability_buckets([0.1, 0.9], [0, 1], n_bins=5)
        assert len(buckets) == 5

    def test_bucket_counts(self) -> None:
        probs = [0.05, 0.15, 0.95]
        labels = [0, 1, 1]
        buckets = compute_reliability_buckets(probs, labels, n_bins=10)
        counts = [b.count for b in buckets]
        assert sum(counts) == 3
        # bin0 [0,0.1) has 0.05 -> count 1
        assert buckets[0].count == 1
        # bin1 [0.1,0.2) has 0.15 -> count 1
        assert buckets[1].count == 1
        # last bin [0.9,1.0] inclusive has 0.95 -> count 1
        assert buckets[9].count == 1

    def test_upper_edge_inclusive(self) -> None:
        # prob exactly 1.0 must land in the final bin.
        buckets = compute_reliability_buckets([1.0], [1], n_bins=10)
        assert buckets[-1].count == 1
        assert buckets[-1].upper == 1.0

    def test_gap_is_abs_diff(self) -> None:
        # Use 0.55 which lands cleanly in bin5 [0.5, 0.6) (0.5 is exact in
        # float, avoiding edge-boundary ambiguity from 0.1*3 rounding).
        buckets = compute_reliability_buckets([0.55], [1], n_bins=10)
        populated = [b for b in buckets if b.count == 1]
        assert len(populated) == 1
        assert populated[0].mean_prob == pytest.approx(0.55)
        assert populated[0].mean_label == pytest.approx(1.0)
        assert populated[0].gap == pytest.approx(0.45)

    def test_empty_bin_midpoint(self) -> None:
        buckets = compute_reliability_buckets([0.05], [0], n_bins=10)
        # bin1 [0.1,0.2) is empty -> mean_prob = midpoint 0.15
        assert buckets[1].count == 0
        assert buckets[1].mean_prob == pytest.approx(0.15)

    def test_invalid_n_bins_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_reliability_buckets([0.1], [0], n_bins=0)

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_reliability_buckets([0.1, 0.2], [0])


# ---------------------------------------------------------------------------
# Calibrator — NONE (fail-closed)
# ===========================================================================


class TestCalibratorNone:
    def test_none_returns_raw_probs(self) -> None:
        cal = Calibrator(CalibrationMethod.NONE)
        raw = [0.1, 0.5, 0.9]
        out = cal.fit_transform(raw, [0, 1, 1])
        assert out == pytest.approx(raw)

    def test_none_transform_before_fit_raises(self) -> None:
        cal = Calibrator(CalibrationMethod.NONE)
        with pytest.raises(RuntimeError):
            cal.transform([0.1])

    def test_none_no_estimator(self) -> None:
        cal = Calibrator(CalibrationMethod.NONE)
        cal.fit([0.1, 0.9], [0, 1])
        assert cal._estimator is None


# ---------------------------------------------------------------------------
# Calibrator — Platt
# ===========================================================================


@_sklearn_skip
class TestCalibratorPlatt:
    def test_fit_transform_roundtrip(self) -> None:
        raw = [0.1, 0.2, 0.3, 0.6, 0.7, 0.9]
        labels = [0, 0, 0, 1, 1, 1]
        cal = Calibrator(CalibrationMethod.PLATT)
        out = cal.fit_transform(raw, labels)
        assert len(out) == len(raw)
        for p in out:
            assert 0.0 <= p <= 1.0

    def test_transform_after_fit(self) -> None:
        raw = [0.1, 0.2, 0.8, 0.9]
        labels = [0, 0, 1, 1]
        cal = Calibrator(CalibrationMethod.PLATT)
        cal.fit(raw, labels)
        out = cal.transform([0.15, 0.85])
        assert len(out) == 2
        assert out[0] < out[1]  # monotonic-ish

    def test_fit_returns_self(self) -> None:
        cal = Calibrator(CalibrationMethod.PLATT)
        assert cal.fit([0.1, 0.9], [0, 1]) is cal

    def test_empty_raises(self) -> None:
        cal = Calibrator(CalibrationMethod.PLATT)
        with pytest.raises(ValueError):
            cal.fit([], [])

    def test_length_mismatch_raises(self) -> None:
        cal = Calibrator(CalibrationMethod.PLATT)
        with pytest.raises(ValueError):
            cal.fit([0.1, 0.2], [0])

    def test_single_class_degenerate_passes_through(self) -> None:
        # All labels 1 -> logistic regression cannot fit two classes;
        # calibrator falls back to pass-through.
        cal = Calibrator(CalibrationMethod.PLATT)
        out = cal.fit_transform([0.1, 0.5, 0.9], [1, 1, 1])
        assert out == pytest.approx([0.1, 0.5, 0.9])


# ---------------------------------------------------------------------------
# Calibrator — Isotonic
# ===========================================================================


@_sklearn_skip
class TestCalibratorIsotonic:
    def test_fit_transform_roundtrip(self) -> None:
        raw = [0.1, 0.2, 0.3, 0.6, 0.7, 0.9]
        labels = [0, 0, 0, 1, 1, 1]
        cal = Calibrator(CalibrationMethod.ISOTONIC)
        out = cal.fit_transform(raw, labels)
        assert len(out) == len(raw)
        for p in out:
            assert 0.0 <= p <= 1.0

    def test_isotonic_monotonic(self) -> None:
        raw = [0.1, 0.2, 0.3, 0.6, 0.7, 0.9]
        labels = [0, 0, 0, 1, 1, 1]
        cal = Calibrator(CalibrationMethod.ISOTONIC)
        cal.fit(raw, labels)
        out = cal.transform([0.05, 0.5, 0.95])
        # Isotonic mapping is non-decreasing.
        assert out[0] <= out[1] <= out[2]

    def test_isotonic_clipped_to_unit(self) -> None:
        raw = [0.1, 0.9]
        labels = [0, 1]
        cal = Calibrator(CalibrationMethod.ISOTONIC)
        cal.fit(raw, labels)
        out = cal.transform([-0.5, 1.5])
        for p in out:
            assert 0.0 <= p <= 1.0


# ---------------------------------------------------------------------------
# Calibrator — constructor validation
# ===========================================================================


class TestCalibratorConstructor:
    def test_invalid_method_type(self) -> None:
        with pytest.raises(TypeError):
            Calibrator("platt")  # type: ignore[arg-type]

    def test_invalid_n_bins(self) -> None:
        with pytest.raises(ValueError):
            Calibrator(CalibrationMethod.PLATT, n_bins=0)


# ---------------------------------------------------------------------------
# Artifact save / load
# ===========================================================================


@_sklearn_skip
class TestCalibratorArtifact:
    def test_save_load_roundtrip_platt(self, tmp_path: Path) -> None:
        raw = [0.1, 0.2, 0.3, 0.6, 0.7, 0.9]
        labels = [0, 0, 0, 1, 1, 1]
        cal = Calibrator(CalibrationMethod.PLATT)
        cal.fit(raw, labels)
        path = tmp_path / "cal_platt.pkl"
        written = cal.save_artifact(str(path))
        assert Path(written).exists()

        loaded = Calibrator.load_artifact(str(path))
        assert loaded.method is CalibrationMethod.PLATT
        out_orig = cal.transform([0.15, 0.85])
        out_loaded = loaded.transform([0.15, 0.85])
        assert out_loaded == pytest.approx(out_orig)

    def test_save_load_roundtrip_isotonic(self, tmp_path: Path) -> None:
        raw = [0.1, 0.2, 0.3, 0.6, 0.7, 0.9]
        labels = [0, 0, 0, 1, 1, 1]
        cal = Calibrator(CalibrationMethod.ISOTONIC)
        cal.fit(raw, labels)
        path = tmp_path / "cal_iso.pkl"
        cal.save_artifact(str(path))

        loaded = Calibrator.load_artifact(str(path))
        assert loaded.method is CalibrationMethod.ISOTONIC
        out_orig = cal.transform([0.05, 0.5, 0.95])
        out_loaded = loaded.transform([0.05, 0.5, 0.95])
        assert out_loaded == pytest.approx(out_orig)

    def test_save_load_none(self, tmp_path: Path) -> None:
        cal = Calibrator(CalibrationMethod.NONE)
        cal.fit([0.1, 0.9], [0, 1])
        path = tmp_path / "cal_none.pkl"
        cal.save_artifact(str(path))
        loaded = Calibrator.load_artifact(str(path))
        assert loaded.transform([0.1, 0.9]) == pytest.approx([0.1, 0.9])

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        cal = Calibrator(CalibrationMethod.NONE)
        cal.fit([0.1, 0.9], [0, 1])
        path = tmp_path / "nested" / "deep" / "cal.pkl"
        written = cal.save_artifact(str(path))
        assert Path(written).exists()

    def test_artifact_is_pickle(self, tmp_path: Path) -> None:
        cal = Calibrator(CalibrationMethod.NONE)
        cal.fit([0.1, 0.9], [0, 1])
        path = tmp_path / "cal.pkl"
        cal.save_artifact(str(path))
        with open(path, "rb") as fh:
            state = pickle.load(fh)
        assert state["method"] is CalibrationMethod.NONE


# ---------------------------------------------------------------------------
# calibrate() entry point
# ===========================================================================


@_sklearn_skip
class TestCalibrateEntryPoint:
    def test_returns_calibration_result(self) -> None:
        raw = [0.1, 0.2, 0.3, 0.6, 0.7, 0.9]
        labels = [0, 0, 0, 1, 1, 1]
        result = calibrate(raw, labels, CalibrationMethod.PLATT)
        assert isinstance(result, CalibrationResult)
        assert result.method is CalibrationMethod.PLATT
        assert len(result.calibrated_probs) == len(raw)

    def test_emits_all_metrics(self) -> None:
        raw = [0.1, 0.2, 0.8, 0.9]
        labels = [0, 0, 1, 1]
        result = calibrate(raw, labels, CalibrationMethod.PLATT, n_bins=4)
        assert isinstance(result.ece, float)
        assert isinstance(result.brier_score, float)
        assert isinstance(result.logloss, float)
        assert len(result.reliability_buckets) == 4

    def test_artifact_path_none_when_not_given(self) -> None:
        result = calibrate([0.1, 0.9], [0, 1], CalibrationMethod.NONE)
        assert result.calibration_artifact_path is None

    def test_artifact_saved_when_path_given(self, tmp_path: Path) -> None:
        path = tmp_path / "cal.pkl"
        result = calibrate(
            [0.1, 0.2, 0.8, 0.9],
            [0, 0, 1, 1],
            CalibrationMethod.PLATT,
            artifact_path=str(path),
        )
        assert result.calibration_artifact_path == str(path)
        assert Path(result.calibration_artifact_path).exists()

    def test_none_method_returns_raw_probs(self) -> None:
        raw = [0.1, 0.5, 0.9]
        result = calibrate(raw, [0, 1, 1], CalibrationMethod.NONE)
        assert result.calibrated_probs == pytest.approx(raw)

    def test_isotonic_entry_point(self) -> None:
        raw = [0.1, 0.2, 0.8, 0.9]
        labels = [0, 0, 1, 1]
        result = calibrate(raw, labels, CalibrationMethod.ISOTONIC)
        assert result.method is CalibrationMethod.ISOTONIC
        assert len(result.calibrated_probs) == 4


# ---------------------------------------------------------------------------
# Calibration improves ECE on synthetic miscalibrated data
# ===========================================================================


@_sklearn_skip
class TestCalibrationImprovesECE:
    def test_platt_improves_ece(self) -> None:
        # Build a synthetic dataset where raw probs are overconfident.
        import random

        rng = random.Random(42)
        n = 400
        # True probabilities spread uniformly; labels drawn from them.
        true_probs = [rng.random() for _ in range(n)]
        labels = [1 if rng.random() < p else 0 for p in true_probs]
        # Overconfident raw probs via steep sigmoid.
        raw = _sigmoid_miscalibrate(true_probs, steepness=5.0)

        raw_ece = compute_ece(raw, labels, n_bins=10)
        result = calibrate(raw, labels, CalibrationMethod.PLATT, n_bins=10)
        assert result.ece < raw_ece

    def test_isotonic_improves_ece(self) -> None:
        import random

        rng = random.Random(7)
        n = 400
        true_probs = [rng.random() for _ in range(n)]
        labels = [1 if rng.random() < p else 0 for p in true_probs]
        raw = _sigmoid_miscalibrate(true_probs, steepness=5.0)

        raw_ece = compute_ece(raw, labels, n_bins=10)
        result = calibrate(raw, labels, CalibrationMethod.ISOTONIC, n_bins=10)
        assert result.ece < raw_ece

    def test_calibration_lowers_brier(self) -> None:
        import random

        rng = random.Random(123)
        n = 300
        true_probs = [rng.random() for _ in range(n)]
        labels = [1 if rng.random() < p else 0 for p in true_probs]
        raw = _sigmoid_miscalibrate(true_probs, steepness=6.0)

        raw_brier = compute_brier_score(raw, labels)
        result = calibrate(raw, labels, CalibrationMethod.ISOTONIC)
        assert result.brier_score <= raw_brier + 1e-9


# ---------------------------------------------------------------------------
# check_calibration_eligibility
# ===========================================================================


class TestCheckCalibrationEligibility:
    def _result(self) -> CalibrationResult:
        return CalibrationResult(
            method=CalibrationMethod.PLATT,
            calibrated_probs=[0.5],
            calibration_artifact_path=None,
            ece=0.0,
            brier_score=0.0,
            logloss=0.0,
            reliability_buckets=[],
        )

    def test_required_with_result_eligible(self) -> None:
        assert check_calibration_eligibility(self._result(), CalibrationPolicy.REQUIRED) is True

    def test_required_without_result_ineligible(self) -> None:
        assert check_calibration_eligibility(None, CalibrationPolicy.REQUIRED) is False

    def test_optional_with_result_eligible(self) -> None:
        assert check_calibration_eligibility(self._result(), CalibrationPolicy.OPTIONAL) is True

    def test_optional_without_result_eligible(self) -> None:
        assert check_calibration_eligibility(None, CalibrationPolicy.OPTIONAL) is True

    def test_none_policy_without_result_eligible(self) -> None:
        assert check_calibration_eligibility(None, CalibrationPolicy.NONE) is True

    def test_none_policy_with_result_ineligible(self) -> None:
        assert check_calibration_eligibility(self._result(), CalibrationPolicy.NONE) is False

    def test_invalid_policy_type_raises(self) -> None:
        with pytest.raises(TypeError):
            check_calibration_eligibility(None, "required")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Edge cases
# ===========================================================================


class TestEdgeCases:
    def test_all_same_probs(self) -> None:
        # All probabilities identical -> single bin populated.
        probs = [0.5, 0.5, 0.5, 0.5]
        labels = [0, 1, 0, 1]
        buckets = compute_reliability_buckets(probs, labels, n_bins=10)
        counts = [b.count for b in buckets]
        assert sum(counts) == 4
        # ECE should be computable without error.
        ece = compute_ece(probs, labels, n_bins=10)
        assert ece >= 0.0

    def test_single_sample(self) -> None:
        result = calibrate([0.7], [1], CalibrationMethod.NONE)
        assert result.calibrated_probs == [0.7]

    def test_empty_probs_metrics(self) -> None:
        assert compute_ece([], [], n_bins=10) == 0.0
        assert compute_brier_score([], []) == 0.0
        assert compute_logloss([], []) == 0.0

    def test_empty_reliability_buckets(self) -> None:
        buckets = compute_reliability_buckets([], [], n_bins=5)
        assert len(buckets) == 5
        assert all(b.count == 0 for b in buckets)

    def test_calibrate_empty_raises(self) -> None:
        # NONE method fit raises on empty input.
        with pytest.raises(ValueError):
            calibrate([], [], CalibrationMethod.NONE)

    def test_probs_at_boundaries(self) -> None:
        probs = [0.0, 1.0, 1.0, 0.0]
        labels = [0, 1, 1, 0]
        result = calibrate(probs, labels, CalibrationMethod.NONE, n_bins=10)
        # Perfect calibration -> ECE 0.
        assert result.ece == pytest.approx(0.0)
