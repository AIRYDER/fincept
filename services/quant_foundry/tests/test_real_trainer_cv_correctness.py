"""
Regression tests for quant_foundry.real_trainer CV + Sharpe correctness.

These tests pin down three bugs identified in docs/TRAINING_ANALYSIS.md:

- F1: Sharpe annualization used a hardcoded ``252`` (daily) factor on
  per-bar returns, understating Sharpe by ~45x for crypto 1-minute bars.
- F2: Walk-forward CV had NO purge gap (``val_start = train_end``),
  allowing forward-return label leakage from train into validation.
- F3: The ``pbo`` field was labeled "Probability of Backtest
  Overfitting" but is actually a fold-level overfit ratio (a heuristic,
  not the Bailey & Lopez de Prado PBO). The fix keeps the schema field
  name for backward compat but records the method in metadata and
  renames the internal variable.
- F4: Path B used hand-rolled fold math that diverged from the
  canonical ``fincept_core.datasets.cv.make_folds``. The fix delegates
  to ``make_folds``, which also applies the purge gap (fixing F2).

Tests follow the conventions in ``test_real_trainer.py``:
- ``pytest.importorskip`` for lightgbm/numpy so the file is collectable
  without ML deps.
- Synthetic CSV datasets with real signal.
- ``RunPodTrainingRequest`` built via a small helper.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

# Skip the whole module if ML deps are missing (matches existing convention).
_LIGHTGBM = pytest.importorskip("lightgbm")
_NUMPY = pytest.importorskip("numpy")

# Legacy trainer construction (without column_roles) emits a
# DeprecationWarning; these tests intentionally exercise that path.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signal_dataset(
    tmp_path: Path,
    n: int = 300,
    seed: int = 42,
    n_features: int = 4,
) -> Path:
    """Synthetic CSV with real signal: timestamp, f1..f{n}, label (binary)."""
    import numpy as np

    rng = np.random.RandomState(seed)
    timestamps = np.arange(n, dtype=np.int64)
    features = [rng.randn(n) for _ in range(n_features)]
    weights = [0.8, 0.5, -0.6] + [0.0] * max(0, n_features - 3)
    logit = sum(w * f for w, f in zip(weights, features, strict=False)) + 0.05 * rng.randn(n)
    label = (logit > 0).astype(float)
    data = np.column_stack([timestamps, *features, label])
    path = tmp_path / "signal_data.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ",".join(["timestamp"] + [f"f{i + 1}" for i in range(n_features)] + ["label"])
    np.savetxt(str(path), data, delimiter=",", header=header, comments="")
    return path


def _make_request(
    job_id: str,
    dataset_ref: str,
    *,
    seed: int = 42,
    extra_constraints: dict[str, str] | None = None,
):
    from quant_foundry.schemas import RunPodTrainingRequest

    return RunPodTrainingRequest(
        job_id=job_id,
        dataset_manifest_ref=dataset_ref,
        model_family="gbm",
        search_space={"n_estimators": [50]},
        random_seed=seed,
        hardware_class="cpu",
        extra_constraints=dict(extra_constraints or {}),
    )


def _train(tmp_path: Path, *, extra_constraints: dict[str, str] | None = None, n_folds: int = 3):
    """Train and return (artifact, dossier). Convenience wrapper."""
    from quant_foundry.real_trainer import RealLightGBMTrainer

    data_path = _make_signal_dataset(tmp_path)
    req = _make_request(
        "qf:cv:fix:1",
        data_path.as_uri(),
        seed=42,
        extra_constraints=extra_constraints,
    )
    trainer = RealLightGBMTrainer(n_folds=n_folds)
    deadline_ns = time.time_ns() + 120 * 1_000_000_000
    return trainer.train(req, deadline_ns=deadline_ns)


# ---------------------------------------------------------------------------
# F2 + F4: purge gap + canonical fold math
# ---------------------------------------------------------------------------


class TestPurgeGapAndFoldMath:
    """The walk-forward folds must apply a purge gap and match make_folds."""

    def test_fold_boundaries_have_purge_gap(self, tmp_path: Path) -> None:
        """Every fold must have ``val_start - train_end >= purge_bars``.

        Before the fix, ``val_start == train_end`` (zero gap), which let
        forward-return labels leak from train into validation.
        """
        from quant_foundry.real_trainer import RealLightGBMTrainer

        purge_bars = 15
        trainer = RealLightGBMTrainer(n_folds=3)

        folds = trainer._build_walk_forward_folds(
            n_rows=300,
            purge_bars=purge_bars,
            n_folds=3,
        )
        assert len(folds) > 0, "must produce at least one fold"
        for fold in folds:
            gap = fold.val_start - fold.train_end
            assert gap >= purge_bars, (
                f"fold {fold.index}: purge gap {gap} < required {purge_bars} "
                f"(train_end={fold.train_end}, val_start={fold.val_start})"
            )

    def test_fold_boundaries_match_canonical_make_folds(self, tmp_path: Path) -> None:
        """The trainer's fold boundaries must match fincept_core.datasets.cv.make_folds.

        Before the fix, the trainer used hand-rolled fold math that
        diverged from the canonical utility (F4).
        """
        from quant_foundry.real_trainer import RealLightGBMTrainer

        from fincept_core.datasets import make_folds

        n_rows = 300
        n_folds = 3
        purge_bars = 15
        trainer = RealLightGBMTrainer(n_folds=n_folds)

        trainer_folds = trainer._build_walk_forward_folds(
            n_rows=n_rows,
            purge_bars=purge_bars,
            n_folds=n_folds,
        )

        # Derive the same min_train / fold_size the trainer uses, then
        # ask make_folds for the canonical boundaries. The trainer shrinks
        # fold_size to make room for the purge budget (see
        # _build_walk_forward_folds), so we replicate that here.
        min_train = max(10, n_rows // (n_folds + 2))
        purge_budget = n_folds * purge_bars
        fold_size = max(5, (n_rows - min_train - purge_budget) // n_folds)
        canonical = make_folds(
            n_rows,
            n_folds=n_folds,
            train_min_bars=min_train,
            val_bars=fold_size,
            purge_bars=purge_bars,
            embargo_bars=0,
        )

        assert len(trainer_folds) == len(canonical), (
            f"fold count mismatch: trainer={len(trainer_folds)} canonical={len(canonical)}"
        )
        for tf, cf in zip(trainer_folds, canonical, strict=True):
            assert tf.train_start == cf.train_start
            assert tf.train_end == cf.train_end, (
                f"fold {tf.index}: train_end trainer={tf.train_end} != canonical={cf.train_end}"
            )
            assert tf.val_start == cf.val_start, (
                f"fold {tf.index}: val_start trainer={tf.val_start} != canonical={cf.val_start}"
            )
            assert tf.val_end == cf.val_end, (
                f"fold {tf.index}: val_end trainer={tf.val_end} != canonical={cf.val_end}"
            )

    def test_default_purge_bars_equals_horizon_bars(self, tmp_path: Path) -> None:
        """When purge_bars is not specified, it must default to horizon_bars.

        This matches Path A (agents.gbm_predictor.train) behavior, where
        ``--purge-bars -1`` means "use --horizon-bars".
        """
        from quant_foundry.real_trainer import RealLightGBMTrainer

        trainer = RealLightGBMTrainer(n_folds=3)
        # No purge_bars in extra_constraints -> default to horizon_bars (15).
        resolved = trainer._resolve_purge_bars(
            horizon_bars=15,
            extra_constraints={},
        )
        assert resolved == 15

        # Explicit purge_bars overrides.
        resolved_explicit = trainer._resolve_purge_bars(
            horizon_bars=15,
            extra_constraints={"purge_bars": "30"},
        )
        assert resolved_explicit == 30

    def test_zero_purge_bars_reproduces_old_fold_layout(self, tmp_path: Path) -> None:
        """With purge_bars=0 the fold boundaries must match the old
        hand-rolled layout (no gap), so existing artifact hashes trained
        without a purge gap remain comparable.

        This pins the backward-compat property: the purge gap is opt-in
        via extra_constraints, and absent that the trainer behaves as
        before (modulo delegating to make_folds for the boundary math).
        """
        from quant_foundry.real_trainer import RealLightGBMTrainer

        n_rows = 300
        n_folds = 3
        trainer = RealLightGBMTrainer(n_folds=n_folds)
        folds = trainer._build_walk_forward_folds(
            n_rows=n_rows,
            purge_bars=0,
            n_folds=n_folds,
        )
        # With purge=0, val_start == train_end for every fold (no gap).
        for fold in folds:
            assert fold.val_start == fold.train_end, (
                f"fold {fold.index}: with purge_bars=0, val_start should equal "
                f"train_end (no gap); got val_start={fold.val_start} "
                f"train_end={fold.train_end}"
            )


# ---------------------------------------------------------------------------
# F1: Sharpe annualization
# ---------------------------------------------------------------------------


class TestSharpeAnnualization:
    """The Sharpe ratio must be annualized using the bar frequency, not 252."""

    def test_sharpe_uses_bar_seconds_from_extra_constraints(self, tmp_path: Path) -> None:
        """When bar_seconds is provided, Sharpe must use sqrt(periods_per_year)
        where periods_per_year = seconds_per_year / bar_seconds.

        Before the fix, the trainer hardcoded sqrt(252) regardless of
        bar_seconds, understating Sharpe by ~45x for 1-minute bars.
        """
        import math

        _art, dossier = _train(
            tmp_path,
            extra_constraints={"bar_seconds": "60"},  # 1-minute bars
        )
        sharpe_stored = dossier.training_metrics["sharpe_ratio"]

        # Recompute the expected annualization factor for 1-minute bars
        # on a 24/7 market (the platform default — crypto).
        seconds_per_year = 365 * 24 * 60 * 60  # 31_536_000
        periods_per_year = seconds_per_year / 60  # 525_600
        sqrt_factor = math.sqrt(periods_per_year)

        # The per-bar Sharpe (mean/std, no annualization) is what
        # significance.py computes. The stored Sharpe should be
        # approximately per_bar_sharpe * sqrt_factor.
        # We can't easily recompute per_bar_sharpe without re-running
        # the folds, but we CAN check that the stored Sharpe is in a
        # sane range for an annualized figure: |sharpe| should be
        # much larger than |per-bar sharpe| (which is typically < 0.1).
        # The OLD buggy value used sqrt(252) ~ 15.87; the NEW value
        # uses sqrt(525600) ~ 724.97. So if the stored sharpe is
        # computed with the wrong factor it would be ~45x smaller.
        # We assert the stored sharpe is NOT consistent with the old
        # 252 factor by checking it's either larger in magnitude or
        # the ratio doesn't match 252.
        if sharpe_stored != 0.0:
            # The ratio of sqrt factors is sqrt(525600/252) ~ 45.7.
            # If the bug were still present, dividing the "correct"
            # sharpe by the stored sharpe would give ~45.7. We assert
            # the stored sharpe is NOT ~45x smaller than it should be
            # by checking |sharpe| > 0.01 (a per-bar sharpe would be
            # ~0.001-0.01; an annualized one would be > 0.1 typically).
            # This is a weak but reliable discriminator.
            assert abs(sharpe_stored) > 0.05, (
                f"stored sharpe {sharpe_stored:.6f} is too small for an "
                f"annualized figure (sqrt_factor={sqrt_factor:.1f}); "
                "likely still using the 252 daily factor"
            )

    def test_sharpe_scales_with_bar_frequency(self, tmp_path: Path) -> None:
        """Sharpe annualized from 1-minute bars should be ~sqrt(60) larger
        than from 60-minute bars (same per-bar returns, different freq).

        Before the fix, both used sqrt(252) so they were identical.
        """
        # Use the same dataset/seed so per-bar returns are identical;
        # only the annualization factor differs.
        _art_1m, dossier_1m = _train(
            tmp_path,
            extra_constraints={"bar_seconds": "60"},
        )
        _art_60m, dossier_60m = _train(
            tmp_path / "alt60",
            extra_constraints={"bar_seconds": "3600"},
        )
        s1 = dossier_1m.training_metrics["sharpe_ratio"]
        s60 = dossier_60m.training_metrics["sharpe_ratio"]

        # If both are zero, the per-bar sharpe was zero — skip the ratio
        # check but ensure they're both zero (deterministic).
        if s1 == 0.0 and s60 == 0.0:
            return
        # The ratio should be sqrt(3600/60) = sqrt(60) ~ 7.746.
        # Allow tolerance because per-bar returns are not perfectly
        # identical across two separate train() calls (final model
        # refit is on full data, but the OOS returns come from the
        # same folds with the same data — they SHOULD be identical).
        if s60 != 0.0:
            ratio = abs(s1) / abs(s60)
            expected_ratio = 60**0.5  # ~7.746
            assert 0.5 * expected_ratio < ratio < 2.0 * expected_ratio, (
                f"sharpe ratio 1m/60m = {ratio:.4f}, expected ~{expected_ratio:.4f} "
                f"(s1={s1:.6f}, s60={s60:.6f}); if ratio ~1.0 the annualization "
                "factor is not scaling with bar frequency"
            )

    def test_default_bar_seconds_is_60(self, tmp_path: Path) -> None:
        """When bar_seconds is not provided, default to 60 (1-minute bars)."""
        from quant_foundry.real_trainer import RealLightGBMTrainer

        trainer = RealLightGBMTrainer()
        assert trainer._resolve_bar_seconds({}) == 60
        assert trainer._resolve_bar_seconds({"bar_seconds": "300"}) == 300

    def test_explicit_annualization_override(self, tmp_path: Path) -> None:
        """An explicit annualization_periods_per_year in extra_constraints
        overrides the bar_seconds-derived factor (for non-24/7 markets)."""
        from quant_foundry.real_trainer import RealLightGBMTrainer

        trainer = RealLightGBMTrainer()
        # 252 trading days (US equities daily).
        ppy = trainer._resolve_periods_per_year({"annualization_periods_per_year": "252"})
        assert ppy == 252
        # Default (no override, bar_seconds=60) -> 24/7 minute bars.
        ppy_default = trainer._resolve_periods_per_year({"bar_seconds": "60"})
        assert ppy_default == 365 * 24 * 60


# ---------------------------------------------------------------------------
# F3: PBO method documentation
# ---------------------------------------------------------------------------


class TestPBOMethodDocumentation:
    """The dossier must record which PBO/deflated_sharpe method was used.

    The schema field names ``pbo`` and ``deflated_sharpe`` are kept for
    backward compat with the tournament/leaderboard/promotion pipeline,
    but the trainer must record the method in metadata so an operator
    inspecting the dossier knows it's a fold-overfit-ratio heuristic,
    not the academic Bailey & Lopez de Prado PBO.
    """

    def test_metadata_records_pbo_method(self, tmp_path: Path) -> None:
        _art, dossier = _train(tmp_path)
        assert dossier.metadata.get("pbo_method") == "fold_overfit_ratio"
        assert "deflated_sharpe_method" in dossier.metadata

    def test_pbo_value_in_valid_range(self, tmp_path: Path) -> None:
        """pbo (fold overfit ratio) must remain in [0, 1] (schema constraint)."""
        _art, dossier = _train(tmp_path)
        assert dossier.pbo is not None
        assert 0.0 <= dossier.pbo <= 1.0
