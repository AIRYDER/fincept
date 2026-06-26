"""Tests for ``fincept_core.datasets.dossier`` (ML dataset evidence spine).

Covers the QA scenarios from todo 10 of the ``ml-dataset-evidence-spine``
plan:
  * Dossier round-trip (JSON-safe + parity with DossierRecord shape).
  * Calibration buckets on synthetic predictions.
  * ECE on synthetic well-calibrated predictions (approx 0.05).
  * Brier on synthetic skewed predictions (0.48 within +-0.02).
  * Failure paths: empty inputs -> empty result (no exception); length
    mismatch -> ``ValueError``.
"""

from __future__ import annotations

import json
import random

import pytest

from fincept_core.datasets import (
    ArtifactManifest,
    DatasetManifest,
    build_calibration_sidecar,
    build_dossier,
)
from fincept_core.datasets.dossier import (
    build_calibration_sidecar as facade_build_calibration_sidecar,
)
from fincept_core.datasets.dossier import build_dossier as facade_build_dossier

# Canonical 64-char lowercase hex SHA-256 used across tests.
_HEX256 = "a" * 64
_HEX256_B = "b" * 64


def _make_artifact() -> ArtifactManifest:
    return ArtifactManifest(
        artifact_id="gbm_predictor.v1",
        sha256=_HEX256,
        size_bytes=4096,
        uri="file:///tmp/gbm_predictor.v1.pkl",
        model_family="lightgbm",
        created_at_ns=1_700_000_000_000_000_000,
        feature_schema_hash=_HEX256,
        label_schema_hash=_HEX256_B,
        code_git_sha="abc123",
        lockfile_hash="def456",
        container_image_digest=None,
    )


def _make_dataset() -> DatasetManifest:
    return DatasetManifest(
        dataset_id="train.v1",
        feature_schema_hash=_HEX256,
        label_schema_hash=_HEX256_B,
        as_of_ts=1_700_000_000_000_000_000,
        universe_hash=_HEX256_B,
        row_count=1000,
        source_vintage_refs=["s3://vintage/2024-01-01"],
    )


# --------------------------------------------------------------------------- #
# Dossier                                                                     #
# --------------------------------------------------------------------------- #


def test_build_dossier_round_trip_json_safe_and_shape() -> None:
    """Dossier is JSON-serializable and matches the DossierRecord field set."""
    artifact = _make_artifact()
    dataset = _make_dataset()
    metrics = {"accuracy": 0.62, "logloss": 0.41, "brier_score": 0.18}

    dossier = build_dossier(
        artifact_manifest=artifact,
        dataset_manifest=dataset,
        training_metrics=metrics,
        blocking_issues=[{"code": "missing_feature", "feature": "mom_z_240m"}],
        extra={
            "random_seed": 42,
            "hardware_class": "gpu-a100",
            "trial_count": 3,
            "settlement_evidence_refs": ["settle/abc"],
            "shadow_prediction_refs": ["shadow/xyz"],
            "dataset_manifest_ref": "s3://manifests/train.v1.json",
        },
    )

    expected_keys = {
        "schema_version",
        "model_id",
        "artifact_manifest_id",
        "artifact_sha256",
        "dataset_manifest_id",
        "dataset_manifest_ref",
        "feature_schema_hash",
        "label_schema_hash",
        "code_git_sha",
        "lockfile_hash",
        "container_image_digest",
        "random_seed",
        "hardware_class",
        "trial_count",
        "training_metrics",
        "status",
        "settlement_evidence_refs",
        "shadow_prediction_refs",
        "blocking_issues",
        "registered_at_ns",
        "content_hash",
    }
    assert set(dossier.keys()) == expected_keys

    # Manifest-derived fields must come from the manifests, not extra.
    assert dossier["artifact_manifest_id"] == "gbm_predictor.v1"
    assert dossier["artifact_sha256"] == _HEX256
    assert dossier["dataset_manifest_id"] == "train.v1"
    assert dossier["feature_schema_hash"] == _HEX256
    assert dossier["label_schema_hash"] == _HEX256_B
    assert dossier["code_git_sha"] == "abc123"
    assert dossier["lockfile_hash"] == "def456"

    # extra-derived fields.
    assert dossier["random_seed"] == 42
    assert dossier["hardware_class"] == "gpu-a100"
    assert dossier["trial_count"] == 3
    assert dossier["settlement_evidence_refs"] == ["settle/abc"]
    assert dossier["shadow_prediction_refs"] == ["shadow/xyz"]
    assert dossier["dataset_manifest_ref"] == "s3://manifests/train.v1.json"

    # Registry-managed defaults.
    assert dossier["status"] == "candidate"
    assert dossier["registered_at_ns"] is None
    assert dossier["content_hash"] == ""
    assert dossier["blocking_issues"] == [
        {"code": "missing_feature", "feature": "mom_z_240m"}
    ]
    assert dossier["training_metrics"] == metrics

    # Must be JSON-safe (round-trip with default encoder).
    encoded = json.dumps(dossier, sort_keys=True)
    assert json.loads(encoded) == dossier


def test_build_dossier_defaults_without_extra() -> None:
    """Without ``extra`` the optional reproducibility fields default cleanly."""
    artifact = _make_artifact()
    dataset = _make_dataset()
    dossier = build_dossier(
        artifact_manifest=artifact,
        dataset_manifest=dataset,
        training_metrics={"accuracy": 0.5},
    )
    assert dossier["random_seed"] is None
    assert dossier["hardware_class"] is None
    assert dossier["trial_count"] == 1
    assert dossier["status"] == "candidate"
    assert dossier["settlement_evidence_refs"] == []
    assert dossier["shadow_prediction_refs"] == []
    assert dossier["dataset_manifest_ref"] is None
    assert dossier["blocking_issues"] == []


def test_build_dossier_facade_reexport_matches_module() -> None:
    """The ``__init__`` facade re-exports the same callables as the module."""
    assert build_dossier is facade_build_dossier
    assert build_calibration_sidecar is facade_build_calibration_sidecar


# --------------------------------------------------------------------------- #
# Calibration sidecar                                                         #
# --------------------------------------------------------------------------- #


def test_calibration_buckets_structure_and_perfect_brier() -> None:
    """Perfect predictions (pred == label, only 0.0/1.0) -> Brier 0.0.

    Uses only the extreme probabilities so that ``round(pred) == label``
    exactly (avoiding banker's-rounding surprises at 0.5) and the squared
    error is identically zero.
    """
    preds = [0.0, 0.0, 1.0, 1.0, 0.0, 1.0]
    labels = [0, 0, 1, 1, 0, 1]
    out = build_calibration_sidecar(
        val_predictions=preds, val_labels=labels, n_buckets=10
    )
    assert set(out.keys()) == {"buckets", "ece", "brier"}
    assert out["brier"] == pytest.approx(0.0, abs=1e-9)
    # ECE is also zero when every populated bucket has mean_pred == mean_actual.
    assert out["ece"] == pytest.approx(0.0, abs=1e-9)
    # Every bucket that has members reports the right keys.
    for b in out["buckets"]:
        assert set(b.keys()) == {"lo", "hi", "mean_pred", "mean_actual", "count"}
        assert 0.0 <= b["lo"] <= b["hi"] <= 1.0
        assert b["count"] >= 1


def test_calibration_ece_well_calibrated_approx_005() -> None:
    """1000 uniformly-sampled predictions with labels drawn from Bernoulli(pred)
    are well-calibrated -> ECE approx 0.05 (within a generous tolerance)."""
    rng = random.Random(20240626)
    preds = [rng.random() for _ in range(1000)]
    labels = [1 if rng.random() < p else 0 for p in preds]
    out = build_calibration_sidecar(
        val_predictions=preds, val_labels=labels, n_buckets=10
    )
    # Well-calibrated -> ECE small.  Allow up to 0.15 for sampling noise.
    assert out["ece"] < 0.15
    # Brier for calibrated predictions ~ E[p(1-p)] which for Uniform(0,1) is 1/6.
    assert out["brier"] == pytest.approx(1.0 / 6.0, abs=0.05)


def test_calibration_brier_skewed_048() -> None:
    """1000 predictions all > 0.5 with 60% true positives -> Brier approx 0.48.

    The plan's hand-calc ``0.4*0.6 + 0.6*0.4 = 0.48`` conflates linear and
    squared distance; with all predictions > 0.5 and a 60/40 positive/negative
    split, Brier = 0.6*(p_pos-1)^2 + 0.4*(p_neg)^2.  Solving for 0.48 with
    ``p_pos = 0.6`` (60% positives predicted at 0.6) gives
    ``0.6*0.16 + 0.4*p_neg^2 = 0.48`` -> ``p_neg = sqrt(0.96) ~= 0.98``.
    So: 600 samples with p=0.6, y=1 and 400 samples with p=0.98, y=0 -- all
    predictions > 0.5, 60% true positives, Brier ~= 0.48 within +-0.02.
    """
    p_neg = 0.96 ** 0.5  # sqrt(0.96) ~= 0.9798
    preds = [0.6] * 600 + [p_neg] * 400
    labels = [1] * 600 + [0] * 400
    out = build_calibration_sidecar(
        val_predictions=preds, val_labels=labels, n_buckets=10
    )
    assert out["brier"] == pytest.approx(0.48, abs=0.02)


def test_calibration_empty_inputs_no_exception() -> None:
    """Empty inputs return the empty-result shape without raising."""
    out = build_calibration_sidecar(
        val_predictions=[], val_labels=[], n_buckets=10
    )
    assert out == {"buckets": [], "ece": 0.0, "brier": 0.0}


def test_calibration_length_mismatch_raises() -> None:
    """Length mismatch between predictions and labels raises ValueError."""
    with pytest.raises(ValueError, match="same length"):
        build_calibration_sidecar(
            val_predictions=[0.1, 0.2, 0.3], val_labels=[0, 1], n_buckets=10
        )


def test_calibration_n_buckets_must_be_positive() -> None:
    """A non-positive ``n_buckets`` is rejected."""
    with pytest.raises(ValueError, match="n_buckets"):
        build_calibration_sidecar(
            val_predictions=[0.5], val_labels=[1], n_buckets=0
        )
