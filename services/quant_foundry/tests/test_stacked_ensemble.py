"""Tests for Phase 8 / T-8.5 — Stacked Ensemble meta-learner.

Covers the acceptance criteria from the spec:

- ``BaseModelSpec``, ``EnsembleManifest``, ``ContributionReport``,
  ``EnsembleCalibrationReport``, ``EnsembleResult`` construction +
  validation (frozen, extra=forbid, non-empty strings, min 2 base
  models, no duplicate families, hash format, rank contiguity).
- ``StackedEnsemble`` with 2-3 base models on synthetic OOF data.
- Meta-learner training on merged OOF predictions (LightGBM +
  logistic_regression families).
- Ensemble prediction (``predict``) with base predictions aligned by
  model family.
- Manifest save / load round-trip.
- Contribution report computation (coefficient-based + permutation).
- Calibration report for REQUIRED / OPTIONAL / NONE policies.
- Fail-closed: missing base artifact, fewer than 2 base models,
  duplicate model families, missing OOF artifact, label mismatch.
- Deterministic ordering of base artifacts (sorted by model_family).
- Ensemble hash determinism.
- Meta-learner only sees OOF predictions (never raw features).
"""

from __future__ import annotations

import json
import math
import os
import pickle
from typing import Any

import pytest

from quant_foundry.oof_artifacts import (
    OOFArtifact,
    OOFRow,
    write_oof_artifact,
)
from quant_foundry.stacked_ensemble import (
    BaseModelSpec,
    ContributionReport,
    EnsembleCalibrationReport,
    EnsembleManifest,
    EnsembleResult,
    StackedEnsemble,
    SUPPORTED_CALIBRATION_POLICIES,
    SUPPORTED_META_LEARNER_FAMILIES,
    compute_contributions,
    compute_ensemble_hash,
)


# ---------------------------------------------------------------------------
# Helpers
# ===========================================================================


def _make_oof_rows(
    model_family: str,
    row_ids: list[str],
    predictions: list[float],
    labels: list[float],
    *,
    fold_id: int = 0,
    horizon: int = 5,
) -> list[OOFRow]:
    """Build a list of OOFRow objects for a single model family."""
    rows: list[OOFRow] = []
    for rid, pred, lbl in zip(row_ids, predictions, labels):
        rows.append(
            OOFRow(
                row_id=rid,
                fold_id=fold_id,
                symbol=rid.split("_")[0] if "_" in rid else "SYM",
                timestamp="2024-04-15T00:00:00+00:00",
                label=float(lbl),
                prediction=float(pred),
                horizon=horizon,
                model_family=model_family,
            )
        )
    return rows


def _write_oof(
    model_family: str,
    row_ids: list[str],
    predictions: list[float],
    labels: list[float],
    output_dir: str,
    *,
    fold_id: int = 0,
) -> str:
    """Write an OOF artifact and return its path."""
    rows = _make_oof_rows(
        model_family, row_ids, predictions, labels, fold_id=fold_id
    )
    path = os.path.join(output_dir, f"oof_{model_family}.json")
    write_oof_artifact(rows=rows, model_family=model_family, output_path=path)
    return path


def _make_base_spec(
    model_family: str,
    output_dir: str,
    row_ids: list[str],
    predictions: list[float],
    labels: list[float],
) -> BaseModelSpec:
    """Build a BaseModelSpec with real OOF artifact + dummy model artifact."""
    oof_path = _write_oof(
        model_family, row_ids, predictions, labels, output_dir
    )
    # Compute the OOF artifact hash by reading it back.
    with open(oof_path, "r", encoding="utf-8") as fh:
        oof_payload = json.load(fh)
    oof_hash = oof_payload["artifact_hash"]

    # Create a dummy base-model artifact (non-empty file).
    artifact_path = os.path.join(output_dir, f"model_{model_family}.pkl")
    with open(artifact_path, "wb") as fh:
        pickle.dump({"model_family": model_family}, fh)

    # Compute its hash.
    import hashlib

    h = hashlib.sha256()
    with open(artifact_path, "rb") as fh:
        h.update(fh.read())
    artifact_hash = h.hexdigest()

    return BaseModelSpec(
        model_family=model_family,
        artifact_path=artifact_path,
        artifact_hash=artifact_hash,
        oof_artifact_path=oof_path,
        oof_artifact_hash=oof_hash,
    )


def _synthetic_ensemble_setup(
    tmp_path: Any,
    n_rows: int = 20,
    n_models: int = 2,
    binary: bool = False,
) -> tuple[list[BaseModelSpec], list[OOFArtifact], list[float]]:
    """Build a synthetic ensemble setup with n base models.

    Returns (base_specs, oof_artifacts, labels).
    """
    row_ids = [f"row_{i:03d}" for i in range(n_rows)]
    if binary:
        labels = [float(i % 2) for i in range(n_rows)]
    else:
        labels = [float(i) / n_rows for i in range(n_rows)]

    families = ["lightgbm", "catboost", "xgboost"][:n_models]
    base_specs: list[BaseModelSpec] = []
    oof_artifacts: list[OOFArtifact] = []
    for idx, fam in enumerate(families):
        # Each model produces slightly different predictions.
        preds = [labels[i] + 0.01 * (idx + 1) * ((-1) ** i) for i in range(n_rows)]
        spec = _make_base_spec(fam, str(tmp_path), row_ids, preds, labels)
        base_specs.append(spec)
        # Read the OOF artifact back.
        from quant_foundry.oof_artifacts import read_oof_artifact

        oof_artifacts.append(read_oof_artifact(spec.oof_artifact_path))

    return base_specs, oof_artifacts, labels


# ---------------------------------------------------------------------------
# BaseModelSpec
# ===========================================================================


class TestBaseModelSpec:
    """Tests for BaseModelSpec construction + validation."""

    def test_valid_construction(self) -> None:
        spec = BaseModelSpec(
            model_family="lightgbm",
            artifact_path="/tmp/model.pkl",
            artifact_hash="abc123",
            oof_artifact_path="/tmp/oof.json",
            oof_artifact_hash="def456",
        )
        assert spec.model_family == "lightgbm"
        assert spec.artifact_path == "/tmp/model.pkl"
        assert spec.artifact_hash == "abc123"
        assert spec.oof_artifact_path == "/tmp/oof.json"
        assert spec.oof_artifact_hash == "def456"

    def test_frozen(self) -> None:
        spec = BaseModelSpec(
            model_family="lightgbm",
            artifact_path="/tmp/m.pkl",
            artifact_hash="a",
            oof_artifact_path="/tmp/o.json",
            oof_artifact_hash="b",
        )
        with pytest.raises(Exception):
            spec.model_family = "catboost"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            BaseModelSpec(
                model_family="lightgbm",
                artifact_path="/tmp/m.pkl",
                artifact_hash="a",
                oof_artifact_path="/tmp/o.json",
                oof_artifact_hash="b",
                extra_field="bad",  # type: ignore[call-arg]
            )

    @pytest.mark.parametrize(
        "field",
        ["model_family", "artifact_path", "artifact_hash", "oof_artifact_path", "oof_artifact_hash"],
    )
    def test_empty_string_rejected(self, field: str) -> None:
        kwargs = {
            "model_family": "lightgbm",
            "artifact_path": "/tmp/m.pkl",
            "artifact_hash": "a",
            "oof_artifact_path": "/tmp/o.json",
            "oof_artifact_hash": "b",
        }
        kwargs[field] = ""
        with pytest.raises(Exception):
            BaseModelSpec(**kwargs)

    @pytest.mark.parametrize(
        "field",
        ["model_family", "artifact_path", "artifact_hash", "oof_artifact_path", "oof_artifact_hash"],
    )
    def test_whitespace_string_rejected(self, field: str) -> None:
        kwargs = {
            "model_family": "lightgbm",
            "artifact_path": "/tmp/m.pkl",
            "artifact_hash": "a",
            "oof_artifact_path": "/tmp/o.json",
            "oof_artifact_hash": "b",
        }
        kwargs[field] = "   "
        with pytest.raises(Exception):
            BaseModelSpec(**kwargs)


# ---------------------------------------------------------------------------
# EnsembleManifest
# ===========================================================================


class TestEnsembleManifest:
    """Tests for EnsembleManifest construction + validation."""

    def _make_base_specs(self, n: int = 2) -> list[BaseModelSpec]:
        families = ["lightgbm", "catboost", "xgboost"][:n]
        return [
            BaseModelSpec(
                model_family=fam,
                artifact_path=f"/tmp/m_{fam}.pkl",
                artifact_hash=f"hash_{fam}",
                oof_artifact_path=f"/tmp/oof_{fam}.json",
                oof_artifact_hash=f"oof_{fam}",
            )
            for fam in families
        ]

    def test_valid_construction(self) -> None:
        manifest = EnsembleManifest(
            base_models=self._make_base_specs(2),
            meta_learner_family="lightgbm",
            meta_learner_artifact_path="/tmp/meta.pkl",
            meta_learner_artifact_hash="a" * 64,
            ensemble_hash="b" * 64,
            created_at="2024-01-01T00:00:00+00:00",
        )
        assert len(manifest.base_models) == 2
        assert manifest.meta_learner_family == "lightgbm"

    def test_frozen(self) -> None:
        manifest = EnsembleManifest(
            base_models=self._make_base_specs(2),
            meta_learner_family="lightgbm",
            meta_learner_artifact_path="/tmp/meta.pkl",
            meta_learner_artifact_hash="a" * 64,
            ensemble_hash="b" * 64,
            created_at="2024-01-01T00:00:00+00:00",
        )
        with pytest.raises(Exception):
            manifest.meta_learner_family = "logistic_regression"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            EnsembleManifest(
                base_models=self._make_base_specs(2),
                meta_learner_family="lightgbm",
                meta_learner_artifact_path="/tmp/meta.pkl",
                meta_learner_artifact_hash="a" * 64,
                ensemble_hash="b" * 64,
                created_at="2024-01-01T00:00:00+00:00",
                extra_field="bad",  # type: ignore[call-arg]
            )

    def test_fewer_than_2_base_models_rejected(self) -> None:
        with pytest.raises(Exception):
            EnsembleManifest(
                base_models=self._make_base_specs(1),
                meta_learner_family="lightgbm",
                meta_learner_artifact_path="/tmp/meta.pkl",
                meta_learner_artifact_hash="a" * 64,
                ensemble_hash="b" * 64,
                created_at="2024-01-01T00:00:00+00:00",
            )

    def test_duplicate_model_families_rejected(self) -> None:
        specs = [
            BaseModelSpec(
                model_family="lightgbm",
                artifact_path="/tmp/m1.pkl",
                artifact_hash="h1",
                oof_artifact_path="/tmp/o1.json",
                oof_artifact_hash="oh1",
            ),
            BaseModelSpec(
                model_family="lightgbm",
                artifact_path="/tmp/m2.pkl",
                artifact_hash="h2",
                oof_artifact_path="/tmp/o2.json",
                oof_artifact_hash="oh2",
            ),
        ]
        with pytest.raises(Exception):
            EnsembleManifest(
                base_models=specs,
                meta_learner_family="lightgbm",
                meta_learner_artifact_path="/tmp/meta.pkl",
                meta_learner_artifact_hash="a" * 64,
                ensemble_hash="b" * 64,
                created_at="2024-01-01T00:00:00+00:00",
            )

    def test_ensemble_hash_must_be_hex64(self) -> None:
        with pytest.raises(Exception):
            EnsembleManifest(
                base_models=self._make_base_specs(2),
                meta_learner_family="lightgbm",
                meta_learner_artifact_path="/tmp/meta.pkl",
                meta_learner_artifact_hash="a" * 64,
                ensemble_hash="not-a-hex-string-of-64-chars!!",
                created_at="2024-01-01T00:00:00+00:00",
            )

    def test_ensemble_hash_wrong_length_rejected(self) -> None:
        with pytest.raises(Exception):
            EnsembleManifest(
                base_models=self._make_base_specs(2),
                meta_learner_family="lightgbm",
                meta_learner_artifact_path="/tmp/meta.pkl",
                meta_learner_artifact_hash="a" * 64,
                ensemble_hash="abc123",
                created_at="2024-01-01T00:00:00+00:00",
            )

    def test_empty_meta_learner_family_rejected(self) -> None:
        with pytest.raises(Exception):
            EnsembleManifest(
                base_models=self._make_base_specs(2),
                meta_learner_family="",
                meta_learner_artifact_path="/tmp/meta.pkl",
                meta_learner_artifact_hash="a" * 64,
                ensemble_hash="b" * 64,
                created_at="2024-01-01T00:00:00+00:00",
            )


# ---------------------------------------------------------------------------
# ContributionReport
# ===========================================================================


class TestContributionReport:
    """Tests for ContributionReport construction + validation."""

    def test_valid_construction(self) -> None:
        report = ContributionReport(
            model_family="lightgbm",
            contribution_score=0.5,
            rank=1,
        )
        assert report.model_family == "lightgbm"
        assert report.contribution_score == 0.5
        assert report.rank == 1

    def test_frozen(self) -> None:
        report = ContributionReport(
            model_family="lightgbm",
            contribution_score=0.5,
            rank=1,
        )
        with pytest.raises(Exception):
            report.rank = 2  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            ContributionReport(
                model_family="lightgbm",
                contribution_score=0.5,
                rank=1,
                extra="bad",  # type: ignore[call-arg]
            )

    def test_negative_score_rejected(self) -> None:
        with pytest.raises(Exception):
            ContributionReport(
                model_family="lightgbm",
                contribution_score=-0.1,
                rank=1,
            )

    def test_zero_rank_rejected(self) -> None:
        with pytest.raises(Exception):
            ContributionReport(
                model_family="lightgbm",
                contribution_score=0.5,
                rank=0,
            )

    def test_empty_model_family_rejected(self) -> None:
        with pytest.raises(Exception):
            ContributionReport(
                model_family="",
                contribution_score=0.5,
                rank=1,
            )


# ---------------------------------------------------------------------------
# EnsembleCalibrationReport
# ===========================================================================


class TestEnsembleCalibrationReport:
    """Tests for EnsembleCalibrationReport construction + validation."""

    def test_valid_with_none_result(self) -> None:
        report = EnsembleCalibrationReport(
            calibration_result=None,
            is_eligible=True,
            policy="optional",
        )
        assert report.calibration_result is None
        assert report.is_eligible is True
        assert report.policy == "optional"

    def test_frozen(self) -> None:
        report = EnsembleCalibrationReport(
            calibration_result=None,
            is_eligible=True,
            policy="none",
        )
        with pytest.raises(Exception):
            report.is_eligible = False  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            EnsembleCalibrationReport(
                calibration_result=None,
                is_eligible=True,
                policy="none",
                extra="bad",  # type: ignore[call-arg]
            )

    def test_unsupported_policy_rejected(self) -> None:
        with pytest.raises(Exception):
            EnsembleCalibrationReport(
                calibration_result=None,
                is_eligible=True,
                policy="always",
            )


# ---------------------------------------------------------------------------
# compute_ensemble_hash
# ===========================================================================


class TestComputeEnsembleHash:
    """Tests for compute_ensemble_hash determinism."""

    def test_deterministic_same_input(self) -> None:
        data = {"a": 1, "b": [1, 2, 3], "c": "hello"}
        h1 = compute_ensemble_hash(data)
        h2 = compute_ensemble_hash(data)
        assert h1 == h2
        assert len(h1) == 64

    def test_order_independent(self) -> None:
        d1 = {"a": 1, "b": 2}
        d2 = {"b": 2, "a": 1}
        assert compute_ensemble_hash(d1) == compute_ensemble_hash(d2)

    def test_excludes_ensemble_hash_field(self) -> None:
        d1 = {"a": 1, "ensemble_hash": "abc"}
        d2 = {"a": 1, "ensemble_hash": "xyz"}
        assert compute_ensemble_hash(d1) == compute_ensemble_hash(d2)

    def test_different_content_different_hash(self) -> None:
        d1 = {"a": 1}
        d2 = {"a": 2}
        assert compute_ensemble_hash(d1) != compute_ensemble_hash(d2)

    def test_returns_hex_string(self) -> None:
        h = compute_ensemble_hash({"x": 1})
        int(h, 16)  # should not raise
        assert h == h.lower()


# ---------------------------------------------------------------------------
# StackedEnsemble — construction + validation
# ===========================================================================


class TestStackedEnsembleConstruction:
    """Tests for StackedEnsemble __init__ validation."""

    def test_valid_construction(self, tmp_path: Any) -> None:
        specs, _, _ = _synthetic_ensemble_setup(tmp_path, n_models=2)
        ensemble = StackedEnsemble(specs)
        assert ensemble.meta_learner_family == "lightgbm"
        assert ensemble.calibration_policy == "optional"
        assert len(ensemble.base_specs) == 2

    def test_fewer_than_2_base_models_rejected(self, tmp_path: Any) -> None:
        specs, _, _ = _synthetic_ensemble_setup(tmp_path, n_models=2)
        with pytest.raises(ValueError, match="at least 2"):
            StackedEnsemble([specs[0]])

    def test_duplicate_families_rejected(self, tmp_path: Any) -> None:
        specs, _, _ = _synthetic_ensemble_setup(tmp_path, n_models=2)
        # Replace the second spec's family with the first's.
        dup = BaseModelSpec(
            model_family=specs[0].model_family,
            artifact_path=specs[1].artifact_path,
            artifact_hash=specs[1].artifact_hash,
            oof_artifact_path=specs[1].oof_artifact_path,
            oof_artifact_hash=specs[1].oof_artifact_hash,
        )
        with pytest.raises(ValueError, match="duplicate"):
            StackedEnsemble([specs[0], dup])

    def test_unsupported_meta_learner_family_rejected(self, tmp_path: Any) -> None:
        specs, _, _ = _synthetic_ensemble_setup(tmp_path, n_models=2)
        with pytest.raises(ValueError, match="unsupported meta_learner_family"):
            StackedEnsemble(specs, meta_learner_family="random_forest")

    def test_unsupported_calibration_policy_rejected(self, tmp_path: Any) -> None:
        specs, _, _ = _synthetic_ensemble_setup(tmp_path, n_models=2)
        with pytest.raises(ValueError, match="unsupported calibration_policy"):
            StackedEnsemble(specs, calibration_policy="always")

    def test_base_specs_sorted_by_family(self, tmp_path: Any) -> None:
        specs, _, _ = _synthetic_ensemble_setup(tmp_path, n_models=3)
        # Pass in reverse order.
        ensemble = StackedEnsemble(list(reversed(specs)))
        families = [s.model_family for s in ensemble.base_specs]
        assert families == sorted(families)

    def test_non_list_base_specs_rejected(self) -> None:
        with pytest.raises(TypeError):
            StackedEnsemble("not-a-list")  # type: ignore[arg-type]

    def test_non_basemodelspec_element_rejected(self) -> None:
        with pytest.raises(TypeError):
            StackedEnsemble(["not-a-spec", "also-not"])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# StackedEnsemble — train_meta_learner
# ===========================================================================


class TestTrainMetaLearner:
    """Tests for meta-learner training on merged OOF predictions."""

    def test_train_lightgbm_meta_learner(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        ensemble = StackedEnsemble(specs, meta_learner_family="lightgbm")
        meta_path = str(tmp_path / "meta.pkl")
        result = ensemble.train_meta_learner(
            oof_arts, labels, meta_learner_artifact_path=meta_path
        )
        assert isinstance(result, EnsembleResult)
        assert len(result.meta_learner_predictions) == 20
        assert len(result.contributions) == 2
        assert "mse" in result.ensemble_metrics
        assert "rmse" in result.ensemble_metrics
        assert os.path.isfile(meta_path)

    def test_train_logistic_regression_meta_learner(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20, binary=True
        )
        ensemble = StackedEnsemble(specs, meta_learner_family="logistic_regression")
        result = ensemble.train_meta_learner(oof_arts, labels)
        assert isinstance(result, EnsembleResult)
        assert len(result.meta_learner_predictions) == 20

    def test_train_three_base_models(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=3, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        result = ensemble.train_meta_learner(oof_arts, labels)
        assert len(result.contributions) == 3
        assert len(result.meta_learner_predictions) == 20

    def test_meta_learner_only_sees_oof_features(self, tmp_path: Any) -> None:
        """The meta-learner feature matrix has exactly n_models columns."""
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=3, n_rows=15
        )
        ensemble = StackedEnsemble(specs)
        ensemble.train_meta_learner(oof_arts, labels)
        # feature_names == base model families (sorted).
        assert ensemble.feature_names == ["catboost", "lightgbm", "xgboost"]
        assert len(ensemble.feature_names) == 3

    def test_label_length_mismatch_rejected(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        with pytest.raises(ValueError, match="labels length"):
            ensemble.train_meta_learner(oof_arts, labels[:10])

    def test_label_value_mismatch_rejected(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        bad_labels = [lbl + 100.0 for lbl in labels]
        with pytest.raises(ValueError, match="label mismatch"):
            ensemble.train_meta_learner(oof_arts, bad_labels)

    def test_wrong_number_of_oof_artifacts_rejected(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=3, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        with pytest.raises(ValueError, match="expected 3 OOF artifacts"):
            ensemble.train_meta_learner(oof_arts[:2], labels)

    def test_oof_artifact_wrong_family_rejected(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        # Replace one OOF artifact with a wrong-family one.
        wrong_rows = _make_oof_rows(
            "wrong_family",
            [r.row_id for r in oof_arts[0].rows],
            [r.prediction for r in oof_arts[0].rows],
            labels,
        )
        wrong_path = str(tmp_path / "oof_wrong.json")
        write_oof_artifact(wrong_rows, "wrong_family", wrong_path)
        from quant_foundry.oof_artifacts import read_oof_artifact

        wrong_art = read_oof_artifact(wrong_path)
        with pytest.raises(ValueError, match="does not match any base model"):
            ensemble.train_meta_learner([wrong_art, oof_arts[1]], labels)

    def test_duplicate_oof_artifact_family_rejected(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        with pytest.raises(ValueError, match="duplicate OOF artifact"):
            ensemble.train_meta_learner([oof_arts[0], oof_arts[0]], labels)

    def test_missing_base_artifact_rejected(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        # Delete a base model artifact file.
        os.remove(specs[0].artifact_path)
        ensemble = StackedEnsemble(specs)
        with pytest.raises(FileNotFoundError):
            ensemble.train_meta_learner(oof_arts, labels)

    def test_missing_oof_artifact_rejected(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        # Delete an OOF artifact file.
        os.remove(specs[0].oof_artifact_path)
        ensemble = StackedEnsemble(specs)
        with pytest.raises(FileNotFoundError):
            ensemble.train_meta_learner(oof_arts, labels)

    def test_empty_base_artifact_rejected(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        # Truncate a base model artifact to 0 bytes.
        with open(specs[0].artifact_path, "w") as fh:
            fh.write("")
        ensemble = StackedEnsemble(specs)
        with pytest.raises(ValueError, match="empty"):
            ensemble.train_meta_learner(oof_arts, labels)

    def test_ensemble_metrics_are_finite(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        result = ensemble.train_meta_learner(oof_arts, labels)
        for key, val in result.ensemble_metrics.items():
            assert math.isfinite(val), f"{key} is not finite: {val}"

    def test_manifest_built_after_training(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        meta_path = str(tmp_path / "meta.pkl")
        result = ensemble.train_meta_learner(
            oof_arts, labels, meta_learner_artifact_path=meta_path
        )
        assert result.manifest.meta_learner_artifact_path == meta_path
        assert result.manifest.meta_learner_artifact_hash != "0" * 64
        assert len(result.manifest.ensemble_hash) == 64

    def test_manifest_in_memory_no_artifact(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        result = ensemble.train_meta_learner(oof_arts, labels)
        assert result.manifest.meta_learner_artifact_path == "<in-memory>"
        assert result.manifest.meta_learner_artifact_hash == "0" * 64


# ---------------------------------------------------------------------------
# StackedEnsemble — predict
# ===========================================================================


class TestPredict:
    """Tests for ensemble prediction."""

    def test_predict_after_training(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        ensemble.train_meta_learner(oof_arts, labels)
        base_preds = {
            "lightgbm": [0.5 + 0.01 * i for i in range(5)],
            "catboost": [0.4 + 0.02 * i for i in range(5)],
        }
        preds = ensemble.predict(base_preds)
        assert len(preds) == 5
        assert all(math.isfinite(p) for p in preds)

    def test_predict_missing_family_rejected(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        ensemble.train_meta_learner(oof_arts, labels)
        with pytest.raises(ValueError, match="missing model families"):
            ensemble.predict({"lightgbm": [0.1, 0.2]})

    def test_predict_extra_family_rejected(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        ensemble.train_meta_learner(oof_arts, labels)
        with pytest.raises(ValueError, match="unexpected model families"):
            ensemble.predict(
                {
                    "lightgbm": [0.1, 0.2],
                    "catboost": [0.3, 0.4],
                    "xgboost": [0.5, 0.6],
                }
            )

    def test_predict_length_mismatch_rejected(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        ensemble.train_meta_learner(oof_arts, labels)
        with pytest.raises(ValueError, match="same length"):
            ensemble.predict(
                {
                    "lightgbm": [0.1, 0.2, 0.3],
                    "catboost": [0.4, 0.5],
                }
            )

    def test_predict_empty_rejected(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        ensemble.train_meta_learner(oof_arts, labels)
        with pytest.raises(ValueError, match="empty"):
            ensemble.predict({"lightgbm": [], "catboost": []})

    def test_predict_without_training_rejected(self, tmp_path: Any) -> None:
        specs, _, _ = _synthetic_ensemble_setup(tmp_path, n_models=2)
        ensemble = StackedEnsemble(specs)
        with pytest.raises(ValueError, match="no meta-learner"):
            ensemble.predict({"lightgbm": [0.1], "catboost": [0.2]})

    def test_predict_loads_from_artifact(self, tmp_path: Any) -> None:
        """predict works after loading a meta-learner from a saved artifact."""
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        meta_path = str(tmp_path / "meta.pkl")
        ensemble.train_meta_learner(
            oof_arts, labels, meta_learner_artifact_path=meta_path
        )
        # Simulate a fresh ensemble that only has the manifest.
        ensemble2 = StackedEnsemble(specs)
        # Load the manifest so predict can find the artifact path.
        manifest = ensemble._manifest
        assert manifest is not None
        ensemble2._manifest = manifest
        preds = ensemble2.predict(
            {
                "lightgbm": [0.5, 0.6],
                "catboost": [0.4, 0.5],
            }
        )
        assert len(preds) == 2


# ---------------------------------------------------------------------------
# StackedEnsemble — manifest save / load
# ===========================================================================


class TestManifestSaveLoad:
    """Tests for manifest save / load round-trip."""

    def test_save_load_round_trip(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        meta_path = str(tmp_path / "meta.pkl")
        ensemble.train_meta_learner(
            oof_arts, labels, meta_learner_artifact_path=meta_path
        )
        manifest_path = str(tmp_path / "manifest.json")
        ensemble.save_manifest(manifest_path)
        assert os.path.isfile(manifest_path)

        loaded = StackedEnsemble.load_manifest(manifest_path)
        assert loaded == ensemble._manifest

    def test_save_manifest_without_training_rejected(self, tmp_path: Any) -> None:
        specs, _, _ = _synthetic_ensemble_setup(tmp_path, n_models=2)
        ensemble = StackedEnsemble(specs)
        with pytest.raises(ValueError, match="no manifest"):
            ensemble.save_manifest(str(tmp_path / "manifest.json"))

    def test_load_manifest_missing_file_rejected(self, tmp_path: Any) -> None:
        with pytest.raises(FileNotFoundError):
            StackedEnsemble.load_manifest(str(tmp_path / "nonexistent.json"))

    def test_load_manifest_empty_file_rejected(self, tmp_path: Any) -> None:
        path = str(tmp_path / "empty.json")
        with open(path, "w") as fh:
            fh.write("")
        with pytest.raises(ValueError, match="empty"):
            StackedEnsemble.load_manifest(path)

    def test_load_manifest_invalid_json_rejected(self, tmp_path: Any) -> None:
        path = str(tmp_path / "bad.json")
        with open(path, "w") as fh:
            fh.write("{not valid json")
        with pytest.raises(ValueError, match="not valid JSON"):
            StackedEnsemble.load_manifest(path)

    def test_manifest_contains_all_base_hashes(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=3, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        result = ensemble.train_meta_learner(oof_arts, labels)
        manifest = result.manifest
        # Every base spec's hash is on the manifest.
        manifest_hashes = {bm.artifact_hash for bm in manifest.base_models}
        spec_hashes = {s.artifact_hash for s in specs}
        assert manifest_hashes == spec_hashes
        # OOF hashes too.
        manifest_oof = {bm.oof_artifact_hash for bm in manifest.base_models}
        spec_oof = {s.oof_artifact_hash for s in specs}
        assert manifest_oof == spec_oof


# ---------------------------------------------------------------------------
# Contribution computation
# ===========================================================================


class TestComputeContributions:
    """Tests for compute_contributions."""

    def test_coefficient_based_contributions(self) -> None:
        """Logistic regression exposes coef_ -> coefficient-based scores."""
        import numpy as np
        from sklearn.linear_model import LinearRegression

        X = np.array([[1, 0], [0, 1], [1, 1], [2, 0]], dtype=float)
        y = np.array([1.0, 2.0, 3.0, 4.0])
        model = LinearRegression()
        model.fit(X, y)
        reports = compute_contributions(model, ["f0", "f1"], X, y)
        assert len(reports) == 2
        assert all(r.contribution_score >= 0 for r in reports)
        # Ranks are contiguous 1..N.
        ranks = sorted(r.rank for r in reports)
        assert ranks == [1, 2]
        # Sorted by score descending.
        assert reports[0].contribution_score >= reports[1].contribution_score

    def test_permutation_based_contributions(self, tmp_path: Any) -> None:
        """LightGBM Booster (no coef_) -> permutation importance."""
        import numpy as np
        from lightgbm import LGBMRegressor

        rng = np.random.default_rng(42)
        X = rng.random((50, 3))
        y = X[:, 0] * 3.0 + X[:, 1] * 0.1 + rng.standard_normal(50) * 0.01
        model = LGBMRegressor(n_estimators=20, verbose=-1, seed=42)
        model.fit(X, y)
        reports = compute_contributions(model, ["a", "b", "c"], X, y)
        assert len(reports) == 3
        ranks = sorted(r.rank for r in reports)
        assert ranks == [1, 2, 3]
        # Feature "a" (col 0, coefficient 3) should rank first.
        assert reports[0].model_family == "a"

    def test_contributions_sorted_descending(self) -> None:
        import numpy as np
        from sklearn.linear_model import LinearRegression

        X = np.array([[1, 0], [0, 1], [1, 1], [2, 0]], dtype=float)
        y = np.array([1.0, 2.0, 3.0, 4.0])
        model = LinearRegression()
        model.fit(X, y)
        reports = compute_contributions(model, ["f0", "f1"], X, y)
        scores = [r.contribution_score for r in reports]
        assert scores == sorted(scores, reverse=True)

    def test_contributions_empty_feature_names_rejected(self) -> None:
        import numpy as np

        X = np.array([[1, 2], [3, 4]])
        y = np.array([1.0, 2.0])
        with pytest.raises(ValueError, match="feature_names must be non-empty"):
            compute_contributions(None, [], X, y)

    def test_contributions_feature_name_mismatch_rejected(self) -> None:
        import numpy as np

        X = np.array([[1, 2], [3, 4]])
        y = np.array([1.0, 2.0])
        with pytest.raises(ValueError, match="feature_names length"):
            compute_contributions(None, ["only_one"], X, y)

    def test_contributions_normalized(self) -> None:
        """Contribution scores from coefficients sum to ~1."""
        import numpy as np
        from sklearn.linear_model import LinearRegression

        X = np.array([[1, 0], [0, 1], [1, 1], [2, 0]], dtype=float)
        y = np.array([1.0, 2.0, 3.0, 4.0])
        model = LinearRegression()
        model.fit(X, y)
        reports = compute_contributions(model, ["f0", "f1"], X, y)
        total = sum(r.contribution_score for r in reports)
        assert abs(total - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# Calibration report — REQUIRED / OPTIONAL / NONE
# ===========================================================================


class TestCalibrationReport:
    """Tests for calibration report under all three policies."""

    def test_optional_policy_binary_includes_calibration(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20, binary=True
        )
        ensemble = StackedEnsemble(
            specs, meta_learner_family="logistic_regression",
            calibration_policy="optional",
        )
        result = ensemble.train_meta_learner(oof_arts, labels)
        assert result.calibration_report.policy == "optional"
        assert result.calibration_report.is_eligible is True
        assert result.calibration_report.calibration_result is not None

    def test_required_policy_binary_includes_calibration(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20, binary=True
        )
        ensemble = StackedEnsemble(
            specs, meta_learner_family="logistic_regression",
            calibration_policy="required",
        )
        result = ensemble.train_meta_learner(oof_arts, labels)
        assert result.calibration_report.policy == "required"
        assert result.calibration_report.is_eligible is True
        assert result.calibration_report.calibration_result is not None

    def test_none_policy_no_calibration(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20, binary=True
        )
        ensemble = StackedEnsemble(
            specs, meta_learner_family="logistic_regression",
            calibration_policy="none",
        )
        result = ensemble.train_meta_learner(oof_arts, labels)
        assert result.calibration_report.policy == "none"
        assert result.calibration_report.calibration_result is None
        assert result.calibration_report.is_eligible is True

    def test_optional_policy_regression_no_calibration(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20, binary=False
        )
        ensemble = StackedEnsemble(
            specs, calibration_policy="optional",
        )
        result = ensemble.train_meta_learner(oof_arts, labels)
        assert result.calibration_report.calibration_result is None
        assert result.calibration_report.is_eligible is True

    def test_required_policy_regression_not_eligible(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20, binary=False
        )
        ensemble = StackedEnsemble(
            specs, calibration_policy="required",
        )
        result = ensemble.train_meta_learner(oof_arts, labels)
        assert result.calibration_report.calibration_result is None
        assert result.calibration_report.is_eligible is False

    def test_none_policy_regression_eligible(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20, binary=False
        )
        ensemble = StackedEnsemble(
            specs, calibration_policy="none",
        )
        result = ensemble.train_meta_learner(oof_arts, labels)
        assert result.calibration_report.calibration_result is None
        assert result.calibration_report.is_eligible is True


# ---------------------------------------------------------------------------
# Deterministic ordering + hash determinism
# ===========================================================================


class TestDeterminism:
    """Tests for deterministic ordering and hash stability."""

    def test_base_artifacts_sorted_by_family(self, tmp_path: Any) -> None:
        specs, _, _ = _synthetic_ensemble_setup(tmp_path, n_models=3)
        # Pass in arbitrary order.
        import random

        shuffled = list(specs)
        random.Random(123).shuffle(shuffled)
        ensemble = StackedEnsemble(shuffled)
        families = [s.model_family for s in ensemble.base_specs]
        assert families == ["catboost", "lightgbm", "xgboost"]

    def test_ensemble_hash_deterministic_same_setup(self, tmp_path: Any) -> None:
        """Two ensembles with the same specs + meta produce the same hash."""
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        # We need to freeze created_at to compare hashes. Build manifests
        # manually with the same timestamp.
        from quant_foundry.stacked_ensemble import EnsembleManifest

        manifest_data_1 = {
            "schema_version": 1,
            "base_models": [bm.model_dump(mode="json") for bm in specs],
            "meta_learner_family": "lightgbm",
            "meta_learner_artifact_path": "/tmp/meta.pkl",
            "meta_learner_artifact_hash": "a" * 64,
            "created_at": "2024-01-01T00:00:00+00:00",
        }
        manifest_data_2 = dict(manifest_data_1)
        h1 = compute_ensemble_hash(manifest_data_1)
        h2 = compute_ensemble_hash(manifest_data_2)
        assert h1 == h2

    def test_ensemble_hash_changes_with_different_base(self, tmp_path: Any) -> None:
        manifest_data_1 = {
            "base_models": [{"model_family": "lightgbm"}],
            "meta_learner_family": "lightgbm",
            "created_at": "2024-01-01",
        }
        manifest_data_2 = {
            "base_models": [{"model_family": "catboost"}],
            "meta_learner_family": "lightgbm",
            "created_at": "2024-01-01",
        }
        assert compute_ensemble_hash(manifest_data_1) != compute_ensemble_hash(manifest_data_2)

    def test_feature_names_deterministic(self, tmp_path: Any) -> None:
        specs, _, _ = _synthetic_ensemble_setup(tmp_path, n_models=3)
        ensemble = StackedEnsemble(list(reversed(specs)))
        assert ensemble.feature_names == ["catboost", "lightgbm", "xgboost"]


# ---------------------------------------------------------------------------
# EnsembleResult validation
# ===========================================================================


class TestEnsembleResultValidation:
    """Tests for EnsembleResult cross-field validators."""

    def test_contributions_must_match_base_families(self, tmp_path: Any) -> None:
        """EnsembleResult rejects contributions that don't match base models."""
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        result = ensemble.train_meta_learner(oof_arts, labels)
        manifest = result.manifest
        # Build a result with a wrong-family contribution.
        bad_contribs = [
            ContributionReport(model_family="wrong", contribution_score=1.0, rank=1),
            ContributionReport(model_family="also_wrong", contribution_score=0.0, rank=2),
        ]
        with pytest.raises(Exception):
            EnsembleResult(
                manifest=manifest,
                meta_learner_predictions=result.meta_learner_predictions,
                contributions=bad_contribs,
                calibration_report=result.calibration_report,
                ensemble_metrics=result.ensemble_metrics,
            )

    def test_contribution_ranks_must_be_contiguous(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        result = ensemble.train_meta_learner(oof_arts, labels)
        manifest = result.manifest
        bad_contribs = [
            ContributionReport(model_family="lightgbm", contribution_score=0.6, rank=1),
            ContributionReport(model_family="catboost", contribution_score=0.4, rank=3),
        ]
        with pytest.raises(Exception, match="contiguous"):
            EnsembleResult(
                manifest=manifest,
                meta_learner_predictions=result.meta_learner_predictions,
                contributions=bad_contribs,
                calibration_report=result.calibration_report,
                ensemble_metrics=result.ensemble_metrics,
            )

    def test_non_finite_predictions_rejected(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=2, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        result = ensemble.train_meta_learner(oof_arts, labels)
        with pytest.raises(Exception):
            EnsembleResult(
                manifest=result.manifest,
                meta_learner_predictions=[float("nan")],
                contributions=result.contributions,
                calibration_report=result.calibration_report,
                ensemble_metrics=result.ensemble_metrics,
            )


# ---------------------------------------------------------------------------
# Integration: full round-trip
# ===========================================================================


class TestIntegration:
    """End-to-end integration tests."""

    def test_full_train_predict_save_load(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=3, n_rows=30
        )
        ensemble = StackedEnsemble(specs, meta_learner_family="lightgbm")
        meta_path = str(tmp_path / "meta_model.pkl")
        result = ensemble.train_meta_learner(
            oof_arts, labels, meta_learner_artifact_path=meta_path
        )

        # Predict.
        base_preds = {
            "lightgbm": [0.5 + 0.01 * i for i in range(10)],
            "catboost": [0.4 + 0.02 * i for i in range(10)],
            "xgboost": [0.3 + 0.015 * i for i in range(10)],
        }
        preds = ensemble.predict(base_preds)
        assert len(preds) == 10

        # Save + reload manifest.
        manifest_path = str(tmp_path / "ensemble_manifest.json")
        ensemble.save_manifest(manifest_path)
        loaded_manifest = StackedEnsemble.load_manifest(manifest_path)
        assert loaded_manifest.ensemble_hash == result.manifest.ensemble_hash
        assert len(loaded_manifest.base_models) == 3

    def test_manifest_lists_all_base_artifact_hashes(self, tmp_path: Any) -> None:
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=3, n_rows=20
        )
        ensemble = StackedEnsemble(specs)
        result = ensemble.train_meta_learner(oof_arts, labels)
        manifest = result.manifest
        spec_hashes = {s.artifact_hash for s in specs}
        manifest_hashes = {bm.artifact_hash for bm in manifest.base_models}
        assert manifest_hashes == spec_hashes

    def test_deterministic_ordering_inference(self, tmp_path: Any) -> None:
        """Inference loads base artifacts in sorted-by-family order."""
        specs, oof_arts, labels = _synthetic_ensemble_setup(
            tmp_path, n_models=3, n_rows=20
        )
        # Pass specs in reverse; the ensemble should still sort them.
        ensemble = StackedEnsemble(list(reversed(specs)))
        result = ensemble.train_meta_learner(oof_arts, labels)
        # The manifest's base_models are in sorted order.
        families = [bm.model_family for bm in result.manifest.base_models]
        assert families == sorted(families)
