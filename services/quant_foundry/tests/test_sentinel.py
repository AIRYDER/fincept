"""
Tests for TASK-0406: Leakage and Overfit Sentinel.

TDD red phase — these tests are written BEFORE the implementation and must
fail with ModuleNotFoundError / ImportError until `sentinel.py` and `pbo.py`
exist.

Acceptance criteria covered (one or more tests per criterion):
- Shuffled-label and future-leak fixtures are flagged as leaking.
- A fold set without purge/embargo is rejected.
- PBO is computed and attached to the dossier.
- A failing sentinel blocks promotion server-side, not just visually.

Additional checks from the spec:
- Time-reversed features fixture flagged as leaking.
- Train/live gap check flags large persistent gap.
- Feature stability check flags wildly unstable features.
- Sentinel receipt emitted per candidate family.

Cross-cutting: the sentinel writes `blocking_issue` entries on dossiers via
`DossierRegistry.add_blocking_issue` (TASK-0403 — my own module), so a
failing sentinel is a hard gate on promotion that the promotion review
queue (TASK-0702) refuses to override without an explicit, recorded human
waiver.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

import pytest

# These imports will fail in the red phase (modules do not exist yet).
from quant_foundry.artifacts import ArtifactRecord
from quant_foundry.dossier import DossierBuilder, DossierRecord
from quant_foundry.pbo import PBOResult, probability_of_backtest_overfitting
from quant_foundry.registry import DossierRegistry
from quant_foundry.sentinel import (
    FeatureStabilityInput,
    FoldSpec,
    LeakageSentinel,
    LeakyFeatureError,
    SentinelCheck,
    SentinelInput,
    SentinelReceipt,
    TrainLiveGapInput,
)

# ---------------------------------------------------------------------------
# Helpers — build minimal dossiers + registries for blocking-issue tests.
# ---------------------------------------------------------------------------


def _make_artifact() -> ArtifactRecord:
    """Build a minimal valid ArtifactRecord for dossier construction."""
    return ArtifactRecord(
        artifact_id="manifest-1",
        sha256="a" * 64,
        size_bytes=1024,
        model_family="test",
        created_at_ns=0,
        feature_schema_hash="f" * 64,
        label_schema_hash="l" * 64,
    )


def _make_dossier(model_id: str = "m1") -> DossierRecord:
    """Build a minimal valid dossier for blocking-issue tests."""
    return DossierBuilder().build(
        artifact=_make_artifact(),
        model_id=model_id,
        dataset_manifest_id="ds-1",
    )


def _make_registry_with_dossier(model_id: str = "m1") -> tuple[DossierRegistry, str]:
    """Create a temp-dir registry with one registered dossier. Returns (registry, path)."""
    tmpdir = tempfile.mkdtemp()
    reg = DossierRegistry(base_dir=tmpdir)
    reg.register(_make_dossier(model_id))
    return reg, tmpdir


# ---------------------------------------------------------------------------
# pbo.py — Probability of Backtest Overfitting (CSCV)
# ===========================================================================


class TestPBO:
    """PBO (Bailey et al. 2017 CSCV) over a candidate family."""

    def test_pbo_returns_result_with_pbo_and_logit(self) -> None:
        # N=4 candidates, T=50 periods. IS/OOS returns matrix.
        # Simple synthetic: candidate 0 is genuinely best, others are noise.
        import random

        rng = random.Random(42)
        n_candidates = 4
        n_periods = 50
        is_returns = []
        oos_returns = []
        for c in range(n_candidates):
            if c == 0:
                # Genuine edge: positive mean.
                is_ret = [rng.gauss(0.001, 0.01) for _ in range(n_periods)]
                oos_ret = [rng.gauss(0.001, 0.01) for _ in range(n_periods)]
            else:
                is_ret = [rng.gauss(0.0, 0.01) for _ in range(n_periods)]
                oos_ret = [rng.gauss(0.0, 0.01) for _ in range(n_periods)]
            is_returns.append(is_ret)
            oos_returns.append(oos_ret)

        result = probability_of_backtest_overfitting(
            is_returns=is_returns,
            oos_returns=oos_returns,
            n_partitions=16,
            seed=42,
        )
        assert isinstance(result, PBOResult)
        assert hasattr(result, "pbo")
        assert hasattr(result, "logit")
        assert 0.0 <= result.pbo <= 1.0

    def test_pbo_low_for_genuine_edge(self) -> None:
        """A family with one genuine edge and few candidates should have low PBO."""
        import random

        rng = random.Random(42)
        n_candidates = 2
        n_periods = 100
        is_returns = []
        oos_returns = []
        for c in range(n_candidates):
            if c == 0:
                is_ret = [rng.gauss(0.002, 0.005) for _ in range(n_periods)]
                oos_ret = [rng.gauss(0.002, 0.005) for _ in range(n_periods)]
            else:
                is_ret = [rng.gauss(0.0, 0.005) for _ in range(n_periods)]
                oos_ret = [rng.gauss(0.0, 0.005) for _ in range(n_periods)]
            is_returns.append(is_ret)
            oos_returns.append(oos_ret)

        result = probability_of_backtest_overfitting(
            is_returns=is_returns, oos_returns=oos_returns,
            n_partitions=16, seed=42,
        )
        # Low PBO means low probability of overfitting.
        assert result.pbo < 0.5

    def test_pbo_high_for_overfit_family(self) -> None:
        """Many candidates with no genuine edge (pure noise) should have high PBO."""
        import random

        rng = random.Random(42)
        n_candidates = 50
        n_periods = 50
        is_returns = []
        oos_returns = []
        for _c in range(n_candidates):
            is_ret = [rng.gauss(0.0, 0.01) for _ in range(n_periods)]
            oos_ret = [rng.gauss(0.0, 0.01) for _ in range(n_periods)]
            is_returns.append(is_ret)
            oos_returns.append(oos_ret)

        result = probability_of_backtest_overfitting(
            is_returns=is_returns, oos_returns=oos_returns,
            n_partitions=16, seed=42,
        )
        # High PBO means high probability of overfitting.
        assert result.pbo > 0.3

    def test_pbo_deterministic_with_fixed_seed(self) -> None:
        import random

        rng = random.Random(99)
        is_ret = [[rng.gauss(0.0, 0.01) for _ in range(50)] for _ in range(10)]
        oos_ret = [[rng.gauss(0.0, 0.01) for _ in range(50)] for _ in range(10)]
        r1 = probability_of_backtest_overfitting(
            is_returns=is_ret, oos_returns=oos_ret, n_partitions=16, seed=7,
        )
        r2 = probability_of_backtest_overfitting(
            is_returns=is_ret, oos_returns=oos_ret, n_partitions=16, seed=7,
        )
        assert r1.pbo == r2.pbo

    def test_pbo_above_threshold_is_flagged(self) -> None:
        """PBO result carries a `flagged` boolean when above a threshold."""
        import random

        rng = random.Random(42)
        is_ret = [[rng.gauss(0.0, 0.01) for _ in range(50)] for _ in range(50)]
        oos_ret = [[rng.gauss(0.0, 0.01) for _ in range(50)] for _ in range(50)]
        result = probability_of_backtest_overfitting(
            is_returns=is_ret, oos_returns=oos_ret,
            n_partitions=16, seed=42, threshold=0.1,
        )
        assert hasattr(result, "flagged")
        assert isinstance(result.flagged, bool)


# ---------------------------------------------------------------------------
# sentinel.py — Negative-control battery
# ===========================================================================


class TestNegativeControlBattery:
    """Shuffle labels, time-reverse features, inject future-leaking feature."""

    def test_shuffled_labels_flagged_as_leaking(self) -> None:
        """A model trained on shuffled labels that still 'finds alpha' is leaking."""
        sentinel = LeakageSentinel(seed=42)
        # Simulate: model claims 5% edge on shuffled labels.
        receipt = sentinel.run_negative_control(
            SentinelInput(
                model_id="m1",
                check=SentinelCheck.SHUFFLED_LABEL,
                claimed_edge=0.05,
                baseline_edge=0.0,
                n_samples=200,
                seed=42,
            )
        )
        assert isinstance(receipt, SentinelReceipt)
        assert receipt.model_id == "m1"
        # A non-trivial edge on shuffled labels is a leakage flag.
        assert any(i.code == "shuffled_label_edge" for i in receipt.issues)
        assert receipt.passed is False

    def test_future_leak_fixture_flagged_as_leaking(self) -> None:
        """A feature with observed_at > decision_time is a future leak."""
        sentinel = LeakageSentinel(seed=42)
        receipt = sentinel.run_negative_control(
            SentinelInput(
                model_id="m1",
                check=SentinelCheck.FUTURE_LEAK,
                feature_observations=[
                    # feature observed AFTER decision time => leak
                    {"decision_time": 100, "observed_at": 200, "feature": "f1"},
                    {"decision_time": 150, "observed_at": 250, "feature": "f1"},
                ],
            )
        )
        assert any(i.code == "future_leak_feature" for i in receipt.issues)
        assert receipt.passed is False

    def test_time_reversed_features_flagged_as_leaking(self) -> None:
        """Time-reversed features that still 'find alpha' indicate the model
        is exploiting temporal structure that shouldn't exist if features are
        truly predictive."""
        sentinel = LeakageSentinel(seed=42)
        receipt = sentinel.run_negative_control(
            SentinelInput(
                model_id="m1",
                check=SentinelCheck.TIME_REVERSE,
                claimed_edge=0.03,
                baseline_edge=0.0,
                n_samples=200,
                seed=42,
            )
        )
        assert any(i.code == "time_reversed_edge" for i in receipt.issues)
        assert receipt.passed is False

    def test_clean_fixture_passes(self) -> None:
        """A clean fixture (no leak, edge within noise) should pass."""
        sentinel = LeakageSentinel(seed=42)
        receipt = sentinel.run_negative_control(
            SentinelInput(
                model_id="m1",
                check=SentinelCheck.SHUFFLED_LABEL,
                claimed_edge=0.0001,  # trivial edge, within noise
                baseline_edge=0.0,
                n_samples=200,
                seed=42,
            )
        )
        assert receipt.passed is True
        assert len(receipt.issues) == 0


# ---------------------------------------------------------------------------
# sentinel.py — Purged-fold verifier
# ===========================================================================


class TestPurgedFoldVerifier:
    """Confirm a dossier's reported folds carry purge + embargo."""

    def test_folds_without_purge_rejected(self) -> None:
        """A fold set without purge/embargo is rejected."""
        sentinel = LeakageSentinel(seed=42)
        # Folds with no gap between train end and val start (no purge).
        folds = [
            FoldSpec(fold_id=0, train_start=0, train_end=100, val_start=100, val_end=150),
            FoldSpec(fold_id=1, train_start=50, train_end=150, val_start=150, val_end=200),
        ]
        receipt = sentinel.verify_purged_folds(
            model_id="m1", folds=folds, purge_gap=5, embargo_gap=3,
        )
        assert receipt.passed is False
        assert any(i.code == "missing_purge_gap" for i in receipt.issues)

    def test_folds_with_purge_and_embargo_pass(self) -> None:
        """Folds with proper purge + embargo pass."""
        sentinel = LeakageSentinel(seed=42)
        # Fold 0: train [0,95), val [100,150). Purge gap = 5 (>= 5). OK.
        # Fold 1: train [155,200), val [205,250). Purge gap = 5. OK.
        # Embargo: fold 1 train_start (155) - fold 0 val_end (150) = 5 >= 3. OK.
        folds = [
            FoldSpec(fold_id=0, train_start=0, train_end=95, val_start=100, val_end=150),
            FoldSpec(fold_id=1, train_start=155, train_end=200, val_start=205, val_end=250),
        ]
        receipt = sentinel.verify_purged_folds(
            model_id="m1", folds=folds, purge_gap=5, embargo_gap=3,
        )
        assert receipt.passed is True
        assert len(receipt.issues) == 0

    def test_folds_with_train_val_overlap_rejected(self) -> None:
        """A training row overlapping a validation label window is rejected."""
        sentinel = LeakageSentinel(seed=42)
        # train_end=105 overlaps val_start=100 (train extends into val).
        folds = [
            FoldSpec(fold_id=0, train_start=0, train_end=105, val_start=100, val_end=150),
        ]
        receipt = sentinel.verify_purged_folds(
            model_id="m1", folds=folds, purge_gap=5, embargo_gap=3,
        )
        assert receipt.passed is False
        assert any(
            i.code in ("train_val_overlap", "missing_purge_gap") for i in receipt.issues
        )


# ---------------------------------------------------------------------------
# sentinel.py — Train/live gap check
# ===========================================================================


class TestTrainLiveGap:
    """Compare in-sample vs. settled live calibration and edge."""

    def test_large_persistent_gap_flagged(self) -> None:
        """A large persistent train/live gap is an overfit flag."""
        sentinel = LeakageSentinel(seed=42)
        receipt = sentinel.check_train_live_gap(
            TrainLiveGapInput(
                model_id="m1",
                in_sample_edge=0.05,
                live_edge=0.001,
                in_sample_brier=0.15,
                live_brier=0.35,
                n_live_settled=50,
            )
        )
        assert receipt.passed is False
        assert any(i.code == "train_live_edge_gap" for i in receipt.issues)

    def test_small_gap_passes(self) -> None:
        """A small train/live gap is fine."""
        sentinel = LeakageSentinel(seed=42)
        receipt = sentinel.check_train_live_gap(
            TrainLiveGapInput(
                model_id="m1",
                in_sample_edge=0.005,
                live_edge=0.004,
                in_sample_brier=0.20,
                live_brier=0.22,
                n_live_settled=50,
            )
        )
        assert receipt.passed is True

    def test_calibration_gap_flagged(self) -> None:
        """A large calibration gap (in-sample vs live Brier) is flagged."""
        sentinel = LeakageSentinel(seed=42)
        receipt = sentinel.check_train_live_gap(
            TrainLiveGapInput(
                model_id="m1",
                in_sample_edge=0.005,
                live_edge=0.004,
                in_sample_brier=0.10,
                live_brier=0.45,
                n_live_settled=50,
            )
        )
        assert receipt.passed is False
        assert any(i.code == "train_live_calibration_gap" for i in receipt.issues)


# ---------------------------------------------------------------------------
# sentinel.py — Feature stability check
# ===========================================================================


class TestFeatureStability:
    """Flag features whose importance or distribution is wildly unstable."""

    def test_stable_features_pass(self) -> None:
        """Features with consistent importance across folds pass."""
        sentinel = LeakageSentinel(seed=42)
        receipt = sentinel.check_feature_stability(
            FeatureStabilityInput(
                model_id="m1",
                feature_importances={
                    "f1": [0.30, 0.32, 0.28, 0.31],
                    "f2": [0.20, 0.19, 0.21, 0.20],
                },
            )
        )
        assert receipt.passed is True

    def test_unstable_feature_flagged(self) -> None:
        """A feature with wildly unstable importance is flagged."""
        sentinel = LeakageSentinel(seed=42)
        receipt = sentinel.check_feature_stability(
            FeatureStabilityInput(
                model_id="m1",
                feature_importances={
                    "f1": [0.01, 0.80, 0.02, 0.79],  # wildly unstable
                    "f2": [0.20, 0.19, 0.21, 0.20],
                },
            )
        )
        assert receipt.passed is False
        assert any(i.code == "unstable_feature_importance" for i in receipt.issues)


# ---------------------------------------------------------------------------
# sentinel.py — Full run + blocking issues on dossier
# ===========================================================================


class TestSentinelFullRun:
    """Run all checks; a failing sentinel blocks promotion server-side."""

    def test_run_all_checks_emits_receipt(self) -> None:
        """Running the full sentinel emits a receipt per candidate family."""
        sentinel = LeakageSentinel(seed=42)
        receipt = sentinel.run(
            SentinelInput(
                model_id="m1",
                check=SentinelCheck.FULL_BATTERY,
                claimed_edge=0.05,
                baseline_edge=0.0,
                n_samples=200,
                seed=42,
                feature_observations=[
                    {"decision_time": 100, "observed_at": 200, "feature": "f1"},
                ],
                folds=[
                    FoldSpec(fold_id=0, train_start=0, train_end=100,
                             val_start=100, val_end=150),
                ],
                purge_gap=5,
                embargo_gap=3,
                train_live_gap=TrainLiveGapInput(
                    model_id="m1",
                    in_sample_edge=0.05, live_edge=0.001,
                    in_sample_brier=0.15, live_brier=0.35,
                    n_live_settled=50,
                ),
                feature_stability=FeatureStabilityInput(
                    model_id="m1",
                    feature_importances={"f1": [0.01, 0.80, 0.02, 0.79]},
                ),
            )
        )
        assert isinstance(receipt, SentinelReceipt)
        assert receipt.model_id == "m1"
        # Multiple issues from multiple checks.
        assert len(receipt.issues) > 0
        assert receipt.passed is False
        # Receipt carries a timestamp and a list of checks run.
        assert hasattr(receipt, "checks_run")
        assert len(receipt.checks_run) > 0

    def test_failing_sentinel_writes_blocking_issue_to_dossier(self) -> None:
        """A failing sentinel writes a blocking_issue on the dossier (hard gate)."""
        reg, tmpdir = _make_registry_with_dossier("m1")
        try:
            sentinel = LeakageSentinel(seed=42)
            receipt = sentinel.run(
                SentinelInput(
                    model_id="m1",
                    check=SentinelCheck.SHUFFLED_LABEL,
                    claimed_edge=0.05,
                    baseline_edge=0.0,
                    n_samples=200,
                    seed=42,
                )
            )
            assert receipt.passed is False
            # Write blocking issues to the dossier registry.
            sentinel.write_blocking_issues(registry=reg, receipt=receipt)
            # The dossier should now have blocking issues.
            updated = reg.get("m1")
            assert len(updated.blocking_issues) > 0
            blocking_codes = [b.get("code", "") for b in updated.blocking_issues]
            assert any("shuffled_label" in c for c in blocking_codes)
            # The source should be "sentinel".
            assert all(b.get("source") == "sentinel" for b in updated.blocking_issues)
        finally:
            # Cleanup temp dir.
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)

    def test_passing_sentinel_does_not_write_blocking_issue(self) -> None:
        """A passing sentinel does NOT write a blocking_issue."""
        reg, tmpdir = _make_registry_with_dossier("m1")
        try:
            sentinel = LeakageSentinel(seed=42)
            receipt = sentinel.run(
                SentinelInput(
                    model_id="m1",
                    check=SentinelCheck.SHUFFLED_LABEL,
                    claimed_edge=0.0001,
                    baseline_edge=0.0,
                    n_samples=200,
                    seed=42,
                )
            )
            assert receipt.passed is True
            sentinel.write_blocking_issues(registry=reg, receipt=receipt)
            updated = reg.get("m1")
            assert len(updated.blocking_issues) == 0
        finally:
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)

    def test_receipt_to_dict_is_json_serializable(self) -> None:
        """The sentinel receipt can be serialized for audit/persistence."""
        import json

        sentinel = LeakageSentinel(seed=42)
        receipt = sentinel.run(
            SentinelInput(
                model_id="m1",
                check=SentinelCheck.SHUFFLED_LABEL,
                claimed_edge=0.05,
                baseline_edge=0.0,
                n_samples=200,
                seed=42,
            )
        )
        d = receipt.to_dict()
        json.dumps(d)
        assert "model_id" in d
        assert "issues" in d
        assert "passed" in d


# ---------------------------------------------------------------------------
# sentinel.py — LeakyFeatureError
# ===========================================================================


class TestLeakyFeatureError:
    """Point-in-time violations raise LeakyFeatureError."""

    def test_leaky_feature_error_is_raised_for_future_leak(self) -> None:
        """A feature observation with observed_at > decision_time raises."""
        with pytest.raises(LeakyFeatureError):
            LeakageSentinel.assert_point_in_time(
                decision_time=100, observed_at=200, feature="f1",
            )

    def test_clean_feature_does_not_raise(self) -> None:
        """A feature observation with observed_at <= decision_time is clean."""
        # Should NOT raise.
        LeakageSentinel.assert_point_in_time(
            decision_time=200, observed_at=100, feature="f1",
        )
        LeakageSentinel.assert_point_in_time(
            decision_time=100, observed_at=100, feature="f1",  # equal is OK
        )


# ---------------------------------------------------------------------------
# Cross-cutting: no secrets in sentinel output
# ===========================================================================


class TestNoSecretsInSentinelOutput:
    """Sentinel output must not leak secrets."""

    @pytest.mark.parametrize("secret_field", [
        "api_key", "token", "secret", "password", "broker_account", "credential",
    ])
    def test_sentinel_input_has_no_secret_fields(self, secret_field: str) -> None:
        """SentinelInput must not have any secret-named field."""
        si_fields = set(SentinelInput.model_fields.keys())
        assert secret_field not in si_fields

    def test_receipt_to_dict_has_no_secret_keys(self) -> None:
        sentinel = LeakageSentinel(seed=42)
        receipt = sentinel.run(
            SentinelInput(
                model_id="m1",
                check=SentinelCheck.SHUFFLED_LABEL,
                claimed_edge=0.05,
                baseline_edge=0.0,
                n_samples=200,
                seed=42,
            )
        )
        d = receipt.to_dict()

        def _has_secret(d: Any, secret_names: set[str]) -> bool:
            if isinstance(d, dict):
                for k, v in d.items():
                    if k.lower() in secret_names:
                        return True
                    if _has_secret(v, secret_names):
                        return True
            elif isinstance(d, list):
                for item in d:
                    if _has_secret(item, secret_names):
                        return True
            return False

        secret_names = {"api_key", "token", "secret", "password",
                        "broker_account", "credential"}
        assert not _has_secret(d, secret_names)
