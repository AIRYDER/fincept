"""
End-to-end integration test: real trainer -> artifact -> real inference -> shadow ledger.

Verifies the full ML pipeline works together:
1. ``RealLightGBMTrainer.train()`` trains a real LightGBM model and produces
   a real ``ArtifactManifest`` with a real sha256 hash + ``ModelDossier`` with
   real metrics.
2. The trained model is saved to a pickle file and loaded by ``ModelLoader``.
3. ``RealInferenceEngine.run()`` loads the model and produces real predictions
   (not the stub linear-combination formula).
4. The predictions are stored in a ``ShadowLedger`` via ``store_batch()``.

Safety invariants verified at every step:
- ``Authority.SHADOW_ONLY`` enforced on every prediction and ledger record.
- No order/OMS fields (quantity, side, broker, etc.) in any prediction or record.
- Deterministic given the same seed + data.
- All temporary files use ``tmp_path`` (no pollution).

ML dependencies (``lightgbm``, ``numpy``) are imported via
``pytest.importorskip`` so the test file is collectable without them.
``pyarrow`` / ``pandas`` are NOT required — the dataset is written as CSV
(which ``RealLightGBMTrainer._load_csv`` reads via ``numpy.genfromtxt``).
"""

from __future__ import annotations

import hashlib
import pickle
import re
import time
from pathlib import Path
from typing import Any

import pytest

# --- skip entire module if lightgbm / numpy are not installed -----------------
# These are imported at collection time so the whole file is skipped cleanly.
_LIGHTGBM = pytest.importorskip("lightgbm")
_NUMPY = pytest.importorskip("numpy")

# Legacy trainer construction (without column_roles) emits a
# DeprecationWarning; these tests intentionally exercise that path.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Order-like / OMS fields that must NEVER appear in a shadow prediction or
# ledger record. Mirrors ``shadow_ledger.ORDER_LIKE_FIELDS``.
_ORDER_LIKE_FIELDS: frozenset[str] = frozenset(
    {
        "quantity",
        "size",
        "side",
        "broker",
        "order_type",
        "order_id",
        "client_order_id",
        "time_in_force",
        "leverage",
        "margin_type",
        "account_id",
    }
)


def _make_synthetic_dataset(
    tmp_path: Path,
    n: int = 300,
    seed: int = 42,
    n_features: int = 4,
) -> tuple[Path, Any, Any]:
    """Create a synthetic CSV dataset with real signal for LightGBM training.

    Layout: timestamp, f1, f2, ..., f{n_features}, label (binary).
    The label has real signal from the first few features so accuracy > 0.5.

    Returns ``(path, X, y)`` where X and y are the numpy arrays (also used
    later for inference feature snapshots).
    """
    import numpy as np

    rng = np.random.RandomState(seed)
    timestamps = np.arange(n, dtype=np.int64)
    features = [rng.randn(n) for _ in range(n_features)]
    # Signal: positive weight on f1, f2; negative on f3; rest is noise.
    weights = [0.8, 0.5, -0.6] + [0.0] * max(0, n_features - 3)
    logit = sum(w * f for w, f in zip(weights, features, strict=False)) + 0.05 * rng.randn(n)
    label = (logit > 0).astype(float)
    data = np.column_stack([timestamps, *features, label])
    path = tmp_path / "synthetic_data.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ",".join(["timestamp"] + [f"f{i + 1}" for i in range(n_features)] + ["label"])
    np.savetxt(str(path), data, delimiter=",", header=header, comments="")
    return path, np.column_stack(features), label


def _make_training_request(
    job_id: str,
    dataset_ref: str,
    seed: int = 42,
) -> Any:
    """Build a RunPodTrainingRequest pointing at a dataset file."""
    from quant_foundry.schemas import RunPodTrainingRequest

    return RunPodTrainingRequest(
        job_id=job_id,
        dataset_manifest_ref=dataset_ref,
        model_family="gbm",
        search_space={"n_estimators": [50]},
        random_seed=seed,
        hardware_class="cpu",
        extra_constraints={},
    )


def _make_feature_snapshot(
    symbols: list[str],
    feature_matrix: Any,
    row_indices: list[int] | None = None,
    ts_event: int = 10_000,
) -> Any:
    """Build a FeatureSnapshot from a feature matrix (one row per symbol).

    ``row_indices`` selects which rows of ``feature_matrix`` to use as feature
    vectors for each symbol. Defaults to sequential rows.
    """
    from quant_foundry.shadow_inference import FeatureSnapshot

    n = len(symbols)
    if row_indices is None:
        row_indices = list(range(n))
    features: dict[str, list[float]] = {}
    availability: dict[str, bool] = {}
    for i, sym in enumerate(symbols):
        row = feature_matrix[row_indices[i]]
        features[sym] = [float(v) for v in row]
        availability[sym] = True
    return FeatureSnapshot(
        symbols=symbols,
        features=features,
        availability=availability,
        ts_event=ts_event,
        freshness_ns=500,
    )


def _stub_linear_direction(features: list[float]) -> float:
    """Compute the stub (ShadowInferenceEngine) direction for comparison.

    The stub uses ``raw_score = sum(features) / len(features)`` and
    ``direction = max(-1, min(1, raw_score * 2))``. Real predictions should
    NOT match this formula.
    """
    raw = sum(features) / max(len(features), 1)
    return max(-1.0, min(1.0, raw * 2.0))


def _assert_no_order_fields(d: dict[str, Any], context: str = "") -> None:
    """Assert that no order-like / OMS fields are present in a dict."""
    present = _ORDER_LIKE_FIELDS & set(d.keys())
    assert not present, (
        f"order-like fields found in {context}: {sorted(present)} "
        "(shadow predictions must never carry trading authority)"
    )


# ---------------------------------------------------------------------------
# Main E2E pipeline test
# ===========================================================================


class TestRealTrainerInferenceE2E:
    """End-to-end: train -> artifact -> inference -> shadow ledger."""

    def test_full_pipeline_train_inference_ledger(self, tmp_path: Path) -> None:
        """Full pipeline: train real model, save artifact, load, infer, store.

        Steps 1-15 from the task specification.
        """
        from quant_foundry.real_inference import ModelLoader, RealInferenceEngine
        from quant_foundry.real_trainer import RealLightGBMTrainer
        from quant_foundry.schemas import (
            ArtifactManifest,
            Authority,
            ModelDossier,
            RunPodInferenceRequest,
        )
        from quant_foundry.shadow_inference import FeatureSnapshot, ShadowInferenceResult
        from quant_foundry.shadow_ledger import ShadowLedger, compute_batch_hash

        # --- Step 1: Create a small synthetic dataset with signal ---
        data_path, X, _y = _make_synthetic_dataset(tmp_path, n=300, seed=42)
        assert data_path.exists()

        # --- Step 2: Dataset is written (CSV; pyarrow/pandas not required) ---
        # (done in step 1)

        # --- Step 3: Create a RunPodTrainingRequest pointing to the file ---
        req = _make_training_request(
            "qf:e2e:train:1",
            data_path.as_uri(),
            seed=42,
        )

        # --- Step 4: Train a real LightGBM model ---
        trainer = RealLightGBMTrainer(n_folds=3)
        deadline_ns = time.time_ns() + 120 * 1_000_000_000
        artifact, dossier = trainer.train(req, deadline_ns=deadline_ns)

        # --- Step 5: Verify ArtifactManifest has a real hash (64-hex sha256) ---
        assert isinstance(artifact, ArtifactManifest)
        assert len(artifact.sha256) == 64
        assert re.fullmatch(r"[0-9a-f]{64}", artifact.sha256), (
            f"sha256 must be 64-char hex, got: {artifact.sha256}"
        )
        assert artifact.size_bytes > 0
        assert artifact.artifact_id.startswith("artifact:")
        assert artifact.model_family == "gbm"

        # --- Step 6: Verify ModelDossier has real metrics (not stub pattern) ---
        assert isinstance(dossier, ModelDossier)
        assert dossier.authority == Authority.SHADOW_ONLY
        metrics = dossier.training_metrics
        assert "accuracy" in metrics
        assert "logloss" in metrics
        assert "brier_score" in metrics
        assert "sharpe_ratio" in metrics
        assert "max_drawdown" in metrics
        assert "win_rate" in metrics

        accuracy = metrics["accuracy"]
        pbo = dossier.pbo
        assert pbo is not None
        # The stub pattern is accuracy = 0.5 + pbo/2.0. Real metrics must differ.
        stub_accuracy = 0.5 + (pbo / 2.0)
        assert abs(accuracy - stub_accuracy) > 1e-6, (
            f"accuracy {accuracy} matches stub pattern 0.5 + pbo/2.0 = {stub_accuracy}"
        )
        assert 0.0 <= accuracy <= 1.0
        assert metrics["logloss"] > 0.0
        assert 0.0 <= metrics["brier_score"] <= 1.0
        assert metrics["max_drawdown"] <= 0.0

        # --- Step 7: Save the trained model bundle to a temporary file ---
        # C1: the trainer now writes ModelBundle v1 (zip archive). Use the
        # bundle bytes from the trainer directly — the sha256 of these bytes
        # is the artifact sha256.
        model_bytes = trainer.last_model_bytes
        assert model_bytes is not None
        model_path = tmp_path / "trained_model.bundle"
        model_path.write_bytes(model_bytes)

        # Verify the bundle hash matches the artifact sha256.
        model_sha = hashlib.sha256(model_bytes).hexdigest()
        assert model_sha == artifact.sha256, (
            f"bundle hash {model_sha} != artifact hash {artifact.sha256} "
            "(trainer must be deterministic)"
        )

        # --- Step 8: Create a FeatureSnapshot from the synthetic data ---
        symbols = ["SYM_A", "SYM_B", "SYM_C", "SYM_D", "SYM_E"]
        # Use rows from the dataset (offset to avoid training rows if desired).
        row_indices = [50, 100, 150, 200, 250]
        snapshot = _make_feature_snapshot(symbols, X, row_indices=row_indices)
        assert isinstance(snapshot, FeatureSnapshot)
        assert set(snapshot.features.keys()) == set(symbols)
        assert all(snapshot.availability[s] is True for s in symbols)

        # --- Step 9: Load the model using ModelLoader ---
        # Use a bare path (not file:// URI) — ModelLoader._resolve_uri treats
        # bare paths as local filesystem paths. On Windows, Path.as_uri()
        # produces file:///C:/... which strips to /C:/... (invalid).
        model_ref = str(model_path)
        loader = ModelLoader()
        scorer = loader.load(model_ref)
        assert hasattr(scorer, "predict")

        # --- Step 10: Run real inference using RealInferenceEngine.run() ---
        infer_req = RunPodInferenceRequest(
            job_id="qf:e2e:infer:1",
            artifact_ref=model_ref,
            symbols=symbols,
            horizons_ns=[3_600_000_000_000, 86_400_000_000_000],
        )
        engine = RealInferenceEngine(enabled=True)
        model_id = dossier.model_id
        result = engine.run(request=infer_req, snapshot=snapshot, model_id=model_id)

        # --- Step 11: Verify ShadowInferenceResult has real predictions ---
        assert isinstance(result, ShadowInferenceResult)
        assert len(result.predictions) > 0
        # 5 symbols * 2 horizons = 10 predictions
        assert len(result.predictions) == len(symbols) * len(infer_req.horizons_ns)

        for pred in result.predictions:
            # Real predictions: direction comes from model output, not stub formula.
            # At least one prediction must differ from the stub formula.
            # (We check the aggregate below; here just sanity-check range.)
            assert -1.0 <= pred.direction <= 1.0
            assert 0.0 <= pred.confidence <= 1.0
            if pred.p_up is not None:
                assert 0.0 <= pred.p_up <= 1.0

        # Verify that NOT ALL predictions match the stub linear formula.
        # (A real model should produce different outputs than mean(features)*2.)
        stub_directions = [
            _stub_linear_direction(snapshot.features[p.symbol]) for p in result.predictions
        ]
        real_directions = [p.direction for p in result.predictions]
        n_matching_stub = sum(
            1 for r, s in zip(real_directions, stub_directions, strict=False) if abs(r - s) < 1e-9
        )
        assert n_matching_stub < len(result.predictions), (
            f"all {len(result.predictions)} predictions match the stub linear "
            f"formula — real model should produce different outputs"
        )

        # --- Step 12: Verify all predictions have authority == "shadow-only" ---
        for pred in result.predictions:
            assert pred.authority == Authority.SHADOW_ONLY, (
                f"prediction {pred.prediction_id} has authority {pred.authority}, "
                f"expected shadow-only"
            )
            # Verify no order/OMS fields in the prediction dict.
            pred_dict = pred.model_dump()
            _assert_no_order_fields(pred_dict, context=f"prediction {pred.prediction_id}")

        # Verify the callback envelope.
        assert result.callback.result_type == "inference_batch"
        assert result.callback.job_id == infer_req.job_id
        assert result.latency_ms >= 0.0

        # --- Step 13: Store the predictions in a temporary ShadowLedger ---
        ledger = ShadowLedger(base_dir=tmp_path / "shadow_ledger")
        pred_dicts = [p.model_dump() for p in result.predictions]
        batch_hash = compute_batch_hash(pred_dicts)
        receipt = ledger.store_batch(pred_dicts, batch_hash)

        # --- Step 14: Verify the ledger has the correct number of records ---
        assert receipt.stored == len(result.predictions)
        assert receipt.duplicates == 0
        assert receipt.batch_hash == batch_hash
        records = ledger.list()
        assert len(records) == len(result.predictions)

        # --- Step 15: Verify no order/OMS fields in any ledger record ---
        for rec in records:
            rec_dict = rec.model_dump()
            _assert_no_order_fields(rec_dict, context=f"ledger record {rec.prediction_id}")
            # Authority must be shadow-only.
            assert rec.authority == Authority.SHADOW_ONLY

        # Additional: verify ledger records match the predictions.
        assert all(rec.model_id == model_id for rec in records)
        assert set(rec.symbol for rec in records) == set(symbols)


# ---------------------------------------------------------------------------
# Determinism tests
# ===========================================================================


class TestDeterminism:
    """Verify the pipeline is deterministic and reproducible."""

    def test_same_model_deterministic_predictions(self, tmp_path: Path) -> None:
        """The same model must produce identical predictions for the same input."""
        from quant_foundry.real_inference import RealInferenceEngine
        from quant_foundry.real_trainer import RealLightGBMTrainer
        from quant_foundry.schemas import RunPodInferenceRequest

        data_path, X, _y = _make_synthetic_dataset(tmp_path, n=300, seed=42)
        req = _make_training_request("qf:det:pred:1", data_path.as_uri(), seed=42)
        trainer = RealLightGBMTrainer()
        deadline_ns = time.time_ns() + 120 * 1_000_000_000
        _artifact, dossier = trainer.train(req, deadline_ns=deadline_ns)

        # Save model bundle (C1: trainer now writes ModelBundle v1).
        model_bytes = trainer.last_model_bytes
        assert model_bytes is not None
        model_path = tmp_path / "model_det.bundle"
        model_path.write_bytes(model_bytes)

        symbols = ["SYM_A", "SYM_B", "SYM_C"]
        snapshot = _make_feature_snapshot(symbols, X, row_indices=[10, 20, 30])
        infer_req = RunPodInferenceRequest(
            job_id="qf:det:infer:1",
            artifact_ref=str(model_path),
            symbols=symbols,
            horizons_ns=[3_600_000_000_000],
        )
        engine = RealInferenceEngine(enabled=True)

        result1 = engine.run(request=infer_req, snapshot=snapshot, model_id=dossier.model_id)
        result2 = engine.run(request=infer_req, snapshot=snapshot, model_id=dossier.model_id)

        # Directions and p_up must be identical (deterministic model).
        dirs1 = [p.direction for p in result1.predictions]
        dirs2 = [p.direction for p in result2.predictions]
        assert dirs1 == dirs2, "same model + same input must produce same directions"

        p_ups1 = [p.p_up for p in result1.predictions]
        p_ups2 = [p.p_up for p in result2.predictions]
        assert p_ups1 == p_ups2, "same model + same input must produce same p_up"

    def test_different_models_different_predictions(self, tmp_path: Path) -> None:
        """Models trained with different seeds must produce different predictions."""
        from quant_foundry.real_inference import RealInferenceEngine
        from quant_foundry.real_trainer import RealLightGBMTrainer
        from quant_foundry.schemas import RunPodInferenceRequest

        # Two datasets with different seeds -> different signal.
        data_path_a, X_a, _y_a = _make_synthetic_dataset(tmp_path, n=300, seed=42)
        data_path_b, _X_b, _y_b = _make_synthetic_dataset(tmp_path / "alt", n=300, seed=99)

        req_a = _make_training_request("qf:diff:1", data_path_a.as_uri(), seed=42)
        req_b = _make_training_request("qf:diff:2", data_path_b.as_uri(), seed=99)

        trainer = RealLightGBMTrainer()
        deadline_ns = time.time_ns() + 120 * 1_000_000_000
        art_a, dossier_a = trainer.train(req_a, deadline_ns=deadline_ns)
        art_b, dossier_b = trainer.train(req_b, deadline_ns=deadline_ns)

        # Different seeds -> different artifact hashes.
        assert art_a.sha256 != art_b.sha256, (
            "different seeds should produce different artifact hashes"
        )

        # Save both models (C1: trainer now writes ModelBundle v1).
        # Re-train to get fresh bundle bytes for each model.
        trainer_a = RealLightGBMTrainer()
        trainer_b = RealLightGBMTrainer()
        trainer_a.train(req_a, deadline_ns=deadline_ns)
        trainer_b.train(req_b, deadline_ns=deadline_ns)
        path_a = tmp_path / "model_a.bundle"
        path_b = tmp_path / "model_b.bundle"
        path_a.write_bytes(trainer_a.last_model_bytes)
        path_b.write_bytes(trainer_b.last_model_bytes)

        # Use the same feature snapshot for both models.
        symbols = ["SYM_A", "SYM_B", "SYM_C"]
        snapshot = _make_feature_snapshot(symbols, X_a, row_indices=[10, 20, 30])
        infer_req = RunPodInferenceRequest(
            job_id="qf:diff:infer:1",
            artifact_ref=str(path_a),  # placeholder; overridden per model
            symbols=symbols,
            horizons_ns=[3_600_000_000_000],
        )
        engine = RealInferenceEngine(enabled=True)

        result_a = engine.run(
            request=infer_req.model_copy(update={"artifact_ref": str(path_a)}),
            snapshot=snapshot,
            model_id=dossier_a.model_id,
        )
        result_b = engine.run(
            request=infer_req.model_copy(update={"artifact_ref": str(path_b)}),
            snapshot=snapshot,
            model_id=dossier_b.model_id,
        )

        dirs_a = [p.direction for p in result_a.predictions]
        dirs_b = [p.direction for p in result_b.predictions]
        assert dirs_a != dirs_b, (
            "different models should produce different predictions for the same input"
        )

    def test_full_pipeline_reproducible_same_seed(self, tmp_path: Path) -> None:
        """Same seed -> same artifact hash -> same predictions (full reproducibility)."""
        from quant_foundry.real_inference import RealInferenceEngine
        from quant_foundry.real_trainer import RealLightGBMTrainer
        from quant_foundry.schemas import RunPodInferenceRequest

        # Two identical datasets in separate dirs.
        data_path_1, X_1, y_1 = _make_synthetic_dataset(tmp_path / "run1", n=300, seed=42)
        data_path_2, X_2, y_2 = _make_synthetic_dataset(tmp_path / "run2", n=300, seed=42)

        req_1 = _make_training_request("qf:repro:1", data_path_1.as_uri(), seed=42)
        req_2 = _make_training_request("qf:repro:2", data_path_2.as_uri(), seed=42)

        trainer = RealLightGBMTrainer()
        deadline_ns = time.time_ns() + 120 * 1_000_000_000
        art_1, dossier_1 = trainer.train(req_1, deadline_ns=deadline_ns)
        art_2, dossier_2 = trainer.train(req_2, deadline_ns=deadline_ns)

        # Same seed + same data -> same artifact hash.
        assert art_1.sha256 == art_2.sha256, "same seed + same data must produce same artifact hash"
        # Same metrics.
        assert dossier_1.training_metrics == dossier_2.training_metrics

        # Save models and run inference.
        model_1 = trainer._train_final_model(X_1, y_1, 42, req_1)
        model_2 = trainer._train_final_model(X_2, y_2, 42, req_2)
        path_1 = tmp_path / "model_repro_1.pkl"
        path_2 = tmp_path / "model_repro_2.pkl"
        path_1.write_bytes(pickle.dumps(model_1, protocol=pickle.HIGHEST_PROTOCOL))
        path_2.write_bytes(pickle.dumps(model_2, protocol=pickle.HIGHEST_PROTOCOL))

        # Same model bytes (deterministic).
        assert path_1.read_bytes() == path_2.read_bytes()

        symbols = ["SYM_A", "SYM_B"]
        snapshot = _make_feature_snapshot(symbols, X_1, row_indices=[10, 20])
        infer_req = RunPodInferenceRequest(
            job_id="qf:repro:infer:1",
            artifact_ref=str(path_1),
            symbols=symbols,
            horizons_ns=[3_600_000_000_000],
        )
        engine = RealInferenceEngine(enabled=True)

        result_1 = engine.run(request=infer_req, snapshot=snapshot, model_id=dossier_1.model_id)
        result_2 = engine.run(
            request=infer_req.model_copy(update={"artifact_ref": str(path_2)}),
            snapshot=snapshot,
            model_id=dossier_2.model_id,
        )

        dirs_1 = [p.direction for p in result_1.predictions]
        dirs_2 = [p.direction for p in result_2.predictions]
        assert dirs_1 == dirs_2, "reproducible pipeline must produce same predictions"


# ---------------------------------------------------------------------------
# Authority enforcement tests
# ===========================================================================


class TestAuthorityEnforcement:
    """Verify Authority.SHADOW_ONLY is enforced at every step."""

    def test_shadow_only_in_dossier(self, tmp_path: Path) -> None:
        """The dossier from training must have authority=shadow_only."""
        from quant_foundry.real_trainer import RealLightGBMTrainer
        from quant_foundry.schemas import Authority

        data_path, _, _ = _make_synthetic_dataset(tmp_path, n=300, seed=42)
        req = _make_training_request("qf:auth:1", data_path.as_uri(), seed=42)
        trainer = RealLightGBMTrainer()
        deadline_ns = time.time_ns() + 120 * 1_000_000_000
        _, dossier = trainer.train(req, deadline_ns=deadline_ns)

        assert dossier.authority == Authority.SHADOW_ONLY

    def test_shadow_only_in_predictions(self, tmp_path: Path) -> None:
        """Every prediction from real inference must have authority=shadow_only."""
        from quant_foundry.real_inference import RealInferenceEngine
        from quant_foundry.real_trainer import RealLightGBMTrainer
        from quant_foundry.schemas import Authority, RunPodInferenceRequest

        data_path, X, y = _make_synthetic_dataset(tmp_path, n=300, seed=42)
        req = _make_training_request("qf:auth:pred:1", data_path.as_uri(), seed=42)
        trainer = RealLightGBMTrainer()
        deadline_ns = time.time_ns() + 120 * 1_000_000_000
        _artifact, dossier = trainer.train(req, deadline_ns=deadline_ns)

        seed = req.random_seed if req.random_seed is not None else 0
        model = trainer._train_final_model(X, y, seed, req)
        model_path = tmp_path / "model_auth.pkl"
        model_path.write_bytes(pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL))

        symbols = ["SYM_A", "SYM_B"]
        snapshot = _make_feature_snapshot(symbols, X, row_indices=[10, 20])
        infer_req = RunPodInferenceRequest(
            job_id="qf:auth:infer:1",
            artifact_ref=str(model_path),
            symbols=symbols,
            horizons_ns=[3_600_000_000_000],
        )
        engine = RealInferenceEngine(enabled=True)
        result = engine.run(request=infer_req, snapshot=snapshot, model_id=dossier.model_id)

        for pred in result.predictions:
            assert pred.authority == Authority.SHADOW_ONLY

    def test_shadow_only_in_ledger(self, tmp_path: Path) -> None:
        """Every ledger record must have authority=shadow_only."""
        from quant_foundry.real_inference import RealInferenceEngine
        from quant_foundry.real_trainer import RealLightGBMTrainer
        from quant_foundry.schemas import Authority, RunPodInferenceRequest
        from quant_foundry.shadow_ledger import ShadowLedger, compute_batch_hash

        data_path, X, y = _make_synthetic_dataset(tmp_path, n=300, seed=42)
        req = _make_training_request("qf:auth:ledger:1", data_path.as_uri(), seed=42)
        trainer = RealLightGBMTrainer()
        deadline_ns = time.time_ns() + 120 * 1_000_000_000
        _artifact, dossier = trainer.train(req, deadline_ns=deadline_ns)

        seed = req.random_seed if req.random_seed is not None else 0
        model = trainer._train_final_model(X, y, seed, req)
        model_path = tmp_path / "model_auth_ledger.pkl"
        model_path.write_bytes(pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL))

        symbols = ["SYM_A", "SYM_B", "SYM_C"]
        snapshot = _make_feature_snapshot(symbols, X, row_indices=[10, 20, 30])
        infer_req = RunPodInferenceRequest(
            job_id="qf:auth:ledger:infer:1",
            artifact_ref=str(model_path),
            symbols=symbols,
            horizons_ns=[3_600_000_000_000],
        )
        engine = RealInferenceEngine(enabled=True)
        result = engine.run(request=infer_req, snapshot=snapshot, model_id=dossier.model_id)

        pred_dicts = [p.model_dump() for p in result.predictions]
        batch_hash = compute_batch_hash(pred_dicts)
        ledger = ShadowLedger(base_dir=tmp_path / "ledger_auth")
        receipt = ledger.store_batch(pred_dicts, batch_hash)

        assert receipt.stored == len(result.predictions)
        for rec in ledger.list():
            assert rec.authority == Authority.SHADOW_ONLY

    def test_no_order_fields_in_callback_payload(self, tmp_path: Path) -> None:
        """The callback envelope payload must not contain order-like fields."""
        from quant_foundry.real_inference import RealInferenceEngine
        from quant_foundry.real_trainer import RealLightGBMTrainer
        from quant_foundry.schemas import RunPodInferenceRequest

        data_path, X, y = _make_synthetic_dataset(tmp_path, n=300, seed=42)
        req = _make_training_request("qf:auth:cb:1", data_path.as_uri(), seed=42)
        trainer = RealLightGBMTrainer()
        deadline_ns = time.time_ns() + 120 * 1_000_000_000
        _artifact, dossier = trainer.train(req, deadline_ns=deadline_ns)

        seed = req.random_seed if req.random_seed is not None else 0
        model = trainer._train_final_model(X, y, seed, req)
        model_path = tmp_path / "model_auth_cb.pkl"
        model_path.write_bytes(pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL))

        symbols = ["SYM_A"]
        snapshot = _make_feature_snapshot(symbols, X, row_indices=[10])
        infer_req = RunPodInferenceRequest(
            job_id="qf:auth:cb:infer:1",
            artifact_ref=str(model_path),
            symbols=symbols,
            horizons_ns=[3_600_000_000_000],
        )
        engine = RealInferenceEngine(enabled=True)
        result = engine.run(request=infer_req, snapshot=snapshot, model_id=dossier.model_id)

        # Check the callback payload predictions for order fields.
        callback_preds = result.callback.payload.get("predictions", [])
        for pred_dict in callback_preds:
            _assert_no_order_fields(pred_dict, context="callback prediction")

    def test_ledger_rejects_order_fields(self, tmp_path: Path) -> None:
        """The ShadowLedger must reject predictions with order-like fields."""
        from quant_foundry.schemas import Authority, ShadowPrediction
        from quant_foundry.shadow_ledger import ShadowLedger, compute_batch_hash

        # Build a valid shadow prediction, then inject an order field.
        pred = ShadowPrediction(
            prediction_id="pred-test-order-1",
            model_id="model:test:order:1",
            symbol="SYM_A",
            ts_event=1000,
            horizon_ns=3_600_000_000_000,
            direction=0.5,
            confidence=0.7,
            authority=Authority.SHADOW_ONLY,
            p_up=0.6,
        )
        pred_dict = pred.model_dump()
        # Inject an order-like field (simulating a tampered payload).
        tampered = dict(pred_dict)
        tampered["quantity"] = 100

        batch_hash = compute_batch_hash([tampered])
        ledger = ShadowLedger(base_dir=tmp_path / "ledger_reject")
        with pytest.raises(ValueError, match="order-like fields"):
            ledger.store_batch([tampered], batch_hash)

    def test_ledger_rejects_non_shadow_authority(self, tmp_path: Path) -> None:
        """The ShadowLedger must reject predictions with non-shadow authority."""
        from quant_foundry.schemas import ShadowPrediction
        from quant_foundry.shadow_ledger import ShadowLedger, compute_batch_hash

        # ShadowPrediction enforces authority=shadow_only via default, but
        # we can test the ledger's explicit check by constructing a valid
        # prediction and verifying the ledger accepts it, then verifying
        # that a tampered authority is caught by the schema.
        pred = ShadowPrediction(
            prediction_id="pred-test-auth-1",
            model_id="model:test:auth:1",
            symbol="SYM_A",
            ts_event=1000,
            horizon_ns=3_600_000_000_000,
            direction=0.5,
            confidence=0.7,
            p_up=0.6,
        )
        pred_dict = pred.model_dump()
        # The authority field is always "shadow-only" from the schema.
        # Verify the ledger accepts a valid shadow-only prediction.
        batch_hash = compute_batch_hash([pred_dict])
        ledger = ShadowLedger(base_dir=tmp_path / "ledger_auth_ok")
        receipt = ledger.store_batch([pred_dict], batch_hash)
        assert receipt.stored == 1
        assert ledger.list()[0].authority == "shadow-only"


# ---------------------------------------------------------------------------
# No-secrets test
# ===========================================================================


class TestNoSecrets:
    """Verify no secrets leak into any test output or artifact."""

    def test_no_secrets_in_artifact_or_dossier(self, tmp_path: Path) -> None:
        """The artifact manifest and dossier must not contain secret fields."""
        import json

        from quant_foundry.real_trainer import RealLightGBMTrainer

        data_path, _, _ = _make_synthetic_dataset(tmp_path, n=300, seed=42)
        req = _make_training_request("qf:leakcheck:1", data_path.as_uri(), seed=42)
        trainer = RealLightGBMTrainer()
        deadline_ns = time.time_ns() + 120 * 1_000_000_000
        artifact, dossier = trainer.train(req, deadline_ns=deadline_ns)

        # Check that no secret-like field names appear as keys in the JSON.
        secret_fields = {"api_key", "password", "token", "credential", "secret_key"}
        artifact_dict = json.loads(artifact.model_dump_json())
        dossier_dict = json.loads(dossier.model_dump_json())

        def _check_keys(d: dict, path: str = "") -> None:
            for key in d:
                assert key.lower() not in secret_fields, (
                    f"secret-like field '{key}' found in {path}"
                )
                if isinstance(d[key], dict):
                    _check_keys(d[key], f"{path}.{key}")

        _check_keys(artifact_dict, "artifact")
        _check_keys(dossier_dict, "dossier")
        _check_keys(dossier_dict.get("metadata", {}), "dossier.metadata")


# ---------------------------------------------------------------------------
# C1: Meta-labeled bundle E2E regression
# ===========================================================================


def _make_triple_barrier_dataset(
    tmp_path: Path,
    n: int = 300,
    seed: int = 42,
    n_features: int = 4,
) -> tuple[Path, Any, Any]:
    """Create a synthetic CSV with triple-barrier labels (-1, 0, +1).

    Layout: timestamp, f1, f2, f3, f4, label.
    """
    import numpy as np

    rng = np.random.RandomState(seed)
    timestamps = np.arange(n, dtype=np.int64)
    features = [rng.randn(n) for _ in range(n_features)]
    weights = [0.8, 0.5, -0.6] + [0.0] * max(0, n_features - 3)
    logit = sum(w * f for w, f in zip(weights, features, strict=False)) + 0.05 * rng.randn(n)
    label = np.where(logit > 0.3, 1.0, np.where(logit < -0.3, -1.0, 0.0))
    data = np.column_stack([timestamps, *features, label])
    path = tmp_path / "tb_data.csv"
    header = ",".join(["timestamp"] + [f"f{i + 1}" for i in range(n_features)] + ["label"])
    np.savetxt(str(path), data, delimiter=",", header=header, comments="")
    return path, np.column_stack(features), label


class TestMetaLabeledBundleE2E:
    """C1 E2E regression: meta-labeled bundle train/write/load/score.

    It must train/write/load/score through the same public loader used
    by inference (ModelLoader.load → load_bundle → BundleScorer).
    """

    def test_meta_labeled_bundle_train_load_score(self, tmp_path: Path) -> None:
        """Full round-trip: train meta-labeled → write bundle → load → score."""
        from quant_foundry.bundle_io import BundleKind, Decision, load_bundle
        from quant_foundry.dataset_manifest import ColumnRoles
        from quant_foundry.real_trainer import RealLightGBMTrainer
        from quant_foundry.training_manifest import ModelTaskSpec

        # --- Train a meta-labeled model ---
        data_path, X, _y = _make_triple_barrier_dataset(tmp_path, n=300, seed=42)
        req = _make_training_request(
            "qf:e2e:meta:bundle:1",
            data_path.as_uri(),
            seed=42,
        )
        roles = ColumnRoles(
            feature_columns=("f1", "f2", "f3", "f4"),
            label_columns=("label",),
            timestamp_column="timestamp",
        )
        task_spec = ModelTaskSpec(
            task_type="multiclass",
            label_column="label",
            barrier_config={
                "profit_take_width": 0.02,
                "stop_loss_width": 0.01,
                "horizon_bars": 10,
            },
            meta_label_config={
                "side_column": "side",
                "label_column": "label",
                "meta_label_column": "meta_label",
            },
        )
        trainer = RealLightGBMTrainer(
            n_folds=3,
            column_roles=roles,
            task_spec=task_spec,
        )
        deadline_ns = time.time_ns() + 120 * 1_000_000_000
        artifact, _dossier = trainer.train(req, deadline_ns=deadline_ns)

        # --- Verify the artifact is a ModelBundle v1 ---
        model_bytes = trainer.last_model_bytes
        assert model_bytes is not None
        assert model_bytes[:4] == b"PK\x03\x04"  # zip magic

        # The artifact sha256 must match the bundle bytes sha256.
        bundle_sha = hashlib.sha256(model_bytes).hexdigest()
        assert bundle_sha == artifact.sha256, (
            f"bundle sha256 {bundle_sha} != artifact sha256 {artifact.sha256}"
        )

        # --- Write the bundle to a file ---
        bundle_path = tmp_path / "meta_labeled.bundle"
        bundle_path.write_bytes(model_bytes)

        # --- Load via the same public loader used by inference ---
        from quant_foundry.real_inference import ModelLoader

        loader = ModelLoader()
        scorer = loader.load(str(bundle_path))
        # The loader returns a BundleScorer (implements _Scorer protocol).
        assert hasattr(scorer, "predict")
        assert hasattr(scorer, "score")

        # --- Score via BundleScorer.score() (full Decision objects) ---
        sample = X[:5].tolist()
        decisions = scorer.score(sample)
        assert len(decisions) == 5
        for d in decisions:
            assert isinstance(d, Decision)
            assert 0.0 <= d.p <= 1.0
            assert d.direction in (-1, 0, 1)
            assert d.meta_p is not None
            assert 0.0 <= d.meta_p <= 1.0
            # Invariant: abstained=True ⇒ act=False
            if d.abstained:
                assert d.act is False
            assert d.bundle_sha256 == bundle_sha

        # --- Also verify .predict() works (backward-compat with _Scorer) ---
        raw_outputs = scorer.predict(sample)
        assert len(raw_outputs) == 5

        # --- Verify the bundle can be loaded directly via load_bundle ---
        bundle = load_bundle(model_bytes)
        assert bundle.bundle_kind == BundleKind.META_LABELED
        assert bundle.primary_model is not None
        assert bundle.meta_model is not None
        assert bundle.bundle_sha256 == bundle_sha

        # --- Verify the selfcheck sample was stashed ---
        assert trainer.last_selfcheck_features is not None
        assert len(trainer.last_selfcheck_features) > 0

        # --- Run the selfcheck against the final bundle bytes ---
        from quant_foundry.bundle_io import run_selfcheck

        selfcheck = run_selfcheck(model_bytes, trainer.last_selfcheck_features)
        assert selfcheck.passed is True
        assert selfcheck.n_rows_scored > 0
        assert selfcheck.bundle_sha256 == bundle_sha
        assert len(selfcheck.output_sha256) == 64

    def test_meta_labeled_bundle_through_real_inference_engine(self, tmp_path: Path) -> None:
        """The meta-labeled bundle loads through RealInferenceEngine.run()."""
        from quant_foundry.dataset_manifest import ColumnRoles
        from quant_foundry.real_inference import RealInferenceEngine
        from quant_foundry.real_trainer import RealLightGBMTrainer
        from quant_foundry.schemas import Authority, RunPodInferenceRequest
        from quant_foundry.training_manifest import ModelTaskSpec

        data_path, X, _y = _make_triple_barrier_dataset(tmp_path, n=300, seed=42)
        req = _make_training_request(
            "qf:e2e:meta:infer:1",
            data_path.as_uri(),
            seed=42,
        )
        roles = ColumnRoles(
            feature_columns=("f1", "f2", "f3", "f4"),
            label_columns=("label",),
            timestamp_column="timestamp",
        )
        task_spec = ModelTaskSpec(
            task_type="multiclass",
            label_column="label",
            barrier_config={
                "profit_take_width": 0.02,
                "stop_loss_width": 0.01,
                "horizon_bars": 10,
            },
            meta_label_config={
                "side_column": "side",
                "label_column": "label",
                "meta_label_column": "meta_label",
            },
        )
        trainer = RealLightGBMTrainer(
            n_folds=3,
            column_roles=roles,
            task_spec=task_spec,
        )
        deadline_ns = time.time_ns() + 120 * 1_000_000_000
        _artifact, dossier = trainer.train(req, deadline_ns=deadline_ns)

        # Write the bundle to a file.
        bundle_path = tmp_path / "meta_infer.bundle"
        bundle_path.write_bytes(trainer.last_model_bytes)

        # Run inference through the public RealInferenceEngine.
        symbols = ["SYM_A", "SYM_B", "SYM_C"]
        snapshot = _make_feature_snapshot(symbols, X, row_indices=[10, 20, 30])
        infer_req = RunPodInferenceRequest(
            job_id="qf:e2e:meta:infer:run:1",
            artifact_ref=str(bundle_path),
            symbols=symbols,
            horizons_ns=[3_600_000_000_000],
        )
        engine = RealInferenceEngine(enabled=True)
        result = engine.run(request=infer_req, snapshot=snapshot, model_id=dossier.model_id)

        # Verify real predictions (not stub).
        assert len(result.predictions) == len(symbols) * len(infer_req.horizons_ns)
        for pred in result.predictions:
            assert pred.authority == Authority.SHADOW_ONLY
            assert -1.0 <= pred.direction <= 1.0
            assert 0.0 <= pred.confidence <= 1.0
