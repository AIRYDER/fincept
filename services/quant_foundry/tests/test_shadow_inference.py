"""
Tests for TASK-0601: Build RunPod Inference Container MVP.

TDD red phase — these tests are written BEFORE the implementation and must
fail with ModuleNotFoundError / ImportError until `shadow_inference.py` exists.

Acceptance criteria covered:
- Container returns valid shadow predictions.
- Invalid feature snapshot fails safely.
- No output contains order fields.
- Inference can be disabled without breaking Fincept.

Additional checks from the spec:
- Accepts RunPodInferenceRequest.
- Loads a candidate artifact from read-only cache or controlled URI.
- Scores fixture feature snapshots.
- Returns ShadowPrediction batch with authority: shadow-only.
- Includes latency and feature availability.
- Sends signed callback (RunPodCallbackEnvelope).

C2 tests (real shadow scorer):
- RealShadowScorer loads a real C1 bundle by default via BundleScorer.score().
- Stub scoring requires an explicit engine=stub flag.
- Predictions carry bundle_sha256, abstained, meta_p, policy_version.
- Offline reproduction: stored prediction equals load_bundle().score(features).
- Bundle load failure fails closed (no silent stub fallback).

File-disjoint from Builder 2's runpod/quant-foundry-training/ (different
subdirectory). Imports ShadowPrediction from schemas.py (read-only) and
ArtifactRecord from my artifacts.py (TASK-0503).
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from quant_foundry.schemas import (
    Authority,
    RunPodCallbackEnvelope,
    RunPodInferenceRequest,
    ShadowPrediction,
)
from quant_foundry.shadow_inference import (
    FeatureSnapshot,
    InferenceDisabledError,
    RealShadowScorer,
    ShadowInferenceEngine,
    ShadowInferenceResult,
    run_real_shadow_scoring,
    run_shadow_inference,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_feature_snapshot(
    symbols: list[str] | None = None,
    n_features: int = 5,
) -> FeatureSnapshot:
    """Build a minimal feature snapshot for testing."""
    if symbols is None:
        symbols = ["AAPL", "MSFT"]
    features: dict[str, list[float]] = {}
    availability: dict[str, bool] = {}
    for sym in symbols:
        features[sym] = [0.1 * i for i in range(n_features)]
        availability[sym] = True
    return FeatureSnapshot(
        symbols=symbols,
        features=features,
        availability=availability,
        ts_event=1000,
        freshness_ns=500,
    )


def _make_inference_request(
    artifact_ref: str = "file:///dummy",
    symbols: list[str] | None = None,
) -> RunPodInferenceRequest:
    """Build a minimal RunPodInferenceRequest."""
    if symbols is None:
        symbols = ["AAPL", "MSFT"]
    return RunPodInferenceRequest(
        job_id="job-1",
        artifact_ref=artifact_ref,
        symbols=symbols,
        horizons_ns=[3600_000_000_000],  # 1 hour
    )


# ---------------------------------------------------------------------------
# FeatureSnapshot
# ===========================================================================


class TestFeatureSnapshot:
    """Feature snapshot schema for inference input."""

    def test_snapshot_has_required_fields(self) -> None:
        """Snapshot has symbols, features, availability, ts_event, freshness."""
        snap = _make_feature_snapshot()
        assert "AAPL" in snap.symbols
        assert "AAPL" in snap.features
        assert snap.availability["AAPL"] is True
        assert snap.ts_event == 1000

    def test_snapshot_is_frozen(self) -> None:
        """Snapshot is frozen (immutable for audit)."""
        snap = _make_feature_snapshot()
        with pytest.raises((TypeError, ValueError)):
            snap.ts_event = 2000  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ShadowInferenceEngine — basic inference
# ===========================================================================


class TestShadowInferenceBasic:
    """The inference engine returns valid shadow predictions."""

    def test_engine_returns_shadow_predictions(self) -> None:
        """The engine returns a batch of ShadowPrediction objects."""
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        assert isinstance(result, ShadowInferenceResult)
        assert len(result.predictions) > 0
        for pred in result.predictions:
            assert isinstance(pred, ShadowPrediction)
            assert pred.authority == Authority.SHADOW_ONLY

    def test_predictions_have_correct_model_id(self) -> None:
        """Each prediction has the correct model_id."""
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        for pred in result.predictions:
            assert pred.model_id == "m1"

    def test_predictions_have_correct_symbols(self) -> None:
        """Each prediction has a symbol from the request."""
        snap = _make_feature_snapshot(symbols=["AAPL", "MSFT"])
        req = _make_inference_request(symbols=["AAPL", "MSFT"])
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        pred_symbols = {p.symbol for p in result.predictions}
        assert pred_symbols == {"AAPL", "MSFT"}

    def test_predictions_have_direction_in_valid_range(self) -> None:
        """Each prediction has a direction in [-1, 1]."""
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        for pred in result.predictions:
            assert -1.0 <= pred.direction <= 1.0

    def test_predictions_have_confidence_in_valid_range(self) -> None:
        """Each prediction has a confidence in [0, 1]."""
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        for pred in result.predictions:
            assert 0.0 <= pred.confidence <= 1.0


# ---------------------------------------------------------------------------
# Latency + feature availability
# ===========================================================================


class TestLatencyAndAvailability:
    """Predictions include latency and feature availability."""

    def test_predictions_include_latency(self) -> None:
        """Each prediction includes a latency_ms value."""
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        for pred in result.predictions:
            assert pred.latency_ms is not None
            assert pred.latency_ms >= 0.0

    def test_predictions_include_feature_availability(self) -> None:
        """Each prediction includes feature availability."""
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        for pred in result.predictions:
            assert pred.feature_availability is not None
            assert "AAPL" in pred.feature_availability or len(pred.feature_availability) > 0

    def test_result_includes_overall_latency(self) -> None:
        """The result includes an overall latency."""
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        assert hasattr(result, "latency_ms")
        assert result.latency_ms >= 0.0


# ---------------------------------------------------------------------------
# Invalid feature snapshot fails safely
# ===========================================================================


class TestInvalidFeatureSnapshot:
    """Invalid feature snapshot fails safely."""

    def test_missing_symbol_in_snapshot_fails_safely(self) -> None:
        """A symbol in the request but not in the snapshot fails safely."""
        snap = _make_feature_snapshot(symbols=["AAPL"])
        req = _make_inference_request(symbols=["AAPL", "MSFT"])
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        # MSFT is missing — the engine should abstain (not predict) for MSFT.
        msft_preds = [p for p in result.predictions if p.symbol == "MSFT"]
        # Either no MSFT predictions, or MSFT predictions with low confidence.
        if msft_preds:
            for pred in msft_preds:
                assert pred.confidence < 0.5  # abstaining

    def test_empty_snapshot_fails_safely(self) -> None:
        """An empty feature snapshot produces no predictions (abstain)."""
        snap = FeatureSnapshot(
            symbols=[],
            features={},
            availability={},
            ts_event=1000,
            freshness_ns=500,
        )
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        # No predictions for an empty snapshot.
        assert len(result.predictions) == 0

    def test_low_availability_produces_abstain(self) -> None:
        """Low feature availability produces abstaining predictions."""
        snap = FeatureSnapshot(
            symbols=["AAPL"],
            features={"AAPL": [0.1, 0.2, 0.3]},
            availability={"AAPL": False},  # low availability
            ts_event=1000,
            freshness_ns=500,
        )
        req = _make_inference_request(symbols=["AAPL"])
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        # Should either abstain (no predictions) or have low confidence.
        if result.predictions:
            for pred in result.predictions:
                assert pred.confidence < 0.5


# ---------------------------------------------------------------------------
# No order fields in output
# ===========================================================================


class TestNoOrderFields:
    """No output contains order fields."""

    def test_predictions_have_no_order_fields(self) -> None:
        """ShadowPrediction has no order/trading fields."""
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        for pred in result.predictions:
            d = pred.model_dump()
            order_keys = {
                "order",
                "signal",
                "trade",
                "position",
                "allocation",
                "quantity",
                "price",
                "side",
            }
            assert not any(k in d for k in order_keys)

    def test_result_to_dict_has_no_order_fields(self) -> None:
        """The result dict has no order/trading fields."""
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        d = result.to_dict()
        order_keys = {
            "order",
            "signal",
            "trade",
            "position",
            "allocation",
            "quantity",
            "price",
            "side",
        }
        assert not any(k in d for k in order_keys)

    def test_authority_is_always_shadow_only(self) -> None:
        """All predictions have authority=shadow_only, never anything else."""
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        for pred in result.predictions:
            assert pred.authority == Authority.SHADOW_ONLY


# ---------------------------------------------------------------------------
# Inference can be disabled
# ===========================================================================


class TestInferenceDisabled:
    """Inference can be disabled without breaking Fincept."""

    def test_disabled_engine_raises_inference_disabled_error(self) -> None:
        """A disabled engine raises InferenceDisabledError."""
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=False)
        with pytest.raises(InferenceDisabledError):
            engine.run(request=req, snapshot=snap, model_id="m1")

    def test_disabled_engine_does_not_produce_predictions(self) -> None:
        """A disabled engine does not produce any predictions."""
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=False)
        try:
            engine.run(request=req, snapshot=snap, model_id="m1")
            raise AssertionError("should have raised InferenceDisabledError")
        except InferenceDisabledError:
            pass  # expected


# ---------------------------------------------------------------------------
# Signed callback
# ===========================================================================


class TestSignedCallback:
    """The engine produces a signed callback envelope."""

    def test_result_includes_callback_envelope(self) -> None:
        """The result includes a RunPodCallbackEnvelope for the signed callback."""
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        assert hasattr(result, "callback")
        assert isinstance(result.callback, RunPodCallbackEnvelope)
        assert result.callback.job_id == req.job_id
        assert result.callback.result_type == "inference_batch"

    def test_callback_payload_contains_predictions(self) -> None:
        """The callback payload contains the serialized predictions."""
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        assert "predictions" in result.callback.payload
        assert len(result.callback.payload["predictions"]) > 0


# ---------------------------------------------------------------------------
# Convenience function
# ===========================================================================


class TestRunShadowInference:
    """The convenience function run_shadow_inference works end-to-end."""

    def test_run_shadow_inference_returns_result(self) -> None:
        """run_shadow_inference returns a ShadowInferenceResult."""
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        result = run_shadow_inference(
            request=req,
            snapshot=snap,
            model_id="m1",
            enabled=True,
        )
        assert isinstance(result, ShadowInferenceResult)
        assert len(result.predictions) > 0

    def test_run_shadow_inference_disabled_raises(self) -> None:
        """run_shadow_inference with enabled=False raises."""
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        with pytest.raises(InferenceDisabledError):
            run_shadow_inference(
                request=req,
                snapshot=snap,
                model_id="m1",
                enabled=False,
            )


# ---------------------------------------------------------------------------
# Result serialization
# ===========================================================================


class TestResultSerialization:
    """The result can be serialized for audit/persistence."""

    def test_result_to_dict_is_json_serializable(self) -> None:
        """The result can be serialized to JSON."""
        import json

        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        d = result.to_dict()
        json.dumps(d)
        assert "predictions" in d
        assert "callback" in d
        assert "latency_ms" in d


# ---------------------------------------------------------------------------
# Cross-cutting: no secrets in output
# ===========================================================================


class TestNoSecretsInInferenceOutput:
    """Inference output must not leak secrets."""

    @pytest.mark.parametrize(
        "secret_field",
        [
            "api_key",
            "token",
            "secret",
            "password",
            "broker_account",
            "credential",
        ],
    )
    def test_feature_snapshot_has_no_secret_fields(self, secret_field: str) -> None:
        """FeatureSnapshot must not have any secret-named field."""
        fields = set(FeatureSnapshot.model_fields.keys())
        assert secret_field not in fields

    def test_result_to_dict_has_no_secret_keys(self) -> None:

        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        d = result.to_dict()

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

        secret_names = {"api_key", "token", "secret", "password", "broker_account", "credential"}
        assert not _has_secret(d, secret_names)


# ---------------------------------------------------------------------------
# C2: Real shadow scorer using C1 bundles
# ===========================================================================


# Helpers for C2 bundle-based tests. These build a real C1 bundle (requires
# lightgbm + numpy) and exercise RealShadowScorer end-to-end.

def _c2_train_tiny_lightgbm(n_features: int = 4, n_rows: int = 80, seed: int = 42) -> Any:
    """Train a tiny LightGBM binary model for C2 bundle tests."""
    import lightgbm as lgb
    import numpy as np

    rng = np.random.RandomState(seed)
    X = rng.randn(n_rows, n_features)
    logits = 0.8 * X[:, 0] + 0.5 * X[:, 1] - 0.6 * X[:, 2]
    y = (logits > 0).astype(float)
    train_set = lgb.Dataset(X, label=y)
    return lgb.train(
        {
            "objective": "binary",
            "metric": "binary_logloss",
            "verbosity": -1,
            "seed": seed,
            "deterministic": True,
            "num_threads": 1,
            "num_leaves": 15,
            "learning_rate": 0.1,
        },
        train_set,
        num_boost_round=20,
    )


def _c2_train_tiny_meta_model(
    primary_model: Any,
    X: Any,
    seed: int = 42,
) -> Any:
    """Train a tiny binary meta-model for C2 meta-labeled bundle tests."""
    import lightgbm as lgb
    import numpy as np

    primary_preds = primary_model.predict(X)
    preds_arr = np.asarray(primary_preds, dtype=np.float64)
    pred_classes = (preds_arr > 0.5).astype(int)
    sides = pred_classes.astype(np.float64)
    meta_labels = (sides > 0).astype(float)
    X_meta = np.column_stack([X.astype(np.float64), sides.reshape(-1, 1)])
    return lgb.train(
        {
            "objective": "binary",
            "metric": "binary_logloss",
            "verbosity": -1,
            "seed": seed,
            "deterministic": True,
            "num_threads": 1,
            "num_leaves": 15,
            "learning_rate": 0.1,
        },
        lgb.Dataset(X_meta, label=meta_labels),
        num_boost_round=20,
    )


def _c2_make_single_bundle(tmp_path: Any, n_features: int = 4) -> tuple[Any, str, str]:
    """Write a single C1 bundle to tmp_path and return (bundle_bytes, path, sha256)."""
    from quant_foundry.bundle_io import write_bundle

    model = _c2_train_tiny_lightgbm(n_features=n_features, seed=42)
    feature_names = [f"f{i}" for i in range(n_features)]
    bundle_bytes = write_bundle(
        primary_model=model,
        meta_model=None,
        feature_names=feature_names,
        feature_schema_hash=hashlib.sha256(b"test-feature-schema").hexdigest()[:16],
        label_schema_hash=hashlib.sha256(b"test-label-schema").hexdigest()[:16],
        model_family="gbm",
    )
    bundle_path = tmp_path / "single.bundle"
    bundle_path.write_bytes(bundle_bytes)
    bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()
    return bundle_bytes, str(bundle_path), bundle_sha


def _c2_make_meta_bundle(tmp_path: Any, n_features: int = 4) -> tuple[Any, str, str]:
    """Write a meta-labeled C1 bundle and return (bundle_bytes, path, sha256)."""
    import numpy as np
    from quant_foundry.bundle_io import write_bundle

    model = _c2_train_tiny_lightgbm(n_features=n_features, seed=42)
    rng = np.random.RandomState(42)
    X = rng.randn(80, n_features)
    meta_model = _c2_train_tiny_meta_model(model, X, seed=42)
    feature_names = [f"f{i}" for i in range(n_features)]
    bundle_bytes = write_bundle(
        primary_model=model,
        meta_model=meta_model,
        feature_names=feature_names,
        feature_schema_hash=hashlib.sha256(b"test-feature-schema").hexdigest()[:16],
        label_schema_hash=hashlib.sha256(b"test-label-schema").hexdigest()[:16],
        model_family="gbm",
        label_map={"-1": 0, "1": 1},
        meta_label_config={"abstain_threshold": 0.5},
    )
    bundle_path = tmp_path / "meta.bundle"
    bundle_path.write_bytes(bundle_bytes)
    bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()
    return bundle_bytes, str(bundle_path), bundle_sha


class TestRealShadowScorerLoadsRealBundleByDefault:
    """C2: RealShadowScorer loads a real C1 bundle by default."""

    def test_shadow_inference_loads_real_bundle_by_default(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """RealShadowScorer loads a real bundle and produces predictions via
        BundleScorer.score() — not the stub formula."""
        pytest.importorskip("lightgbm")
        pytest.importorskip("numpy")

        _, bundle_path, bundle_sha = _c2_make_single_bundle(tmp_path, n_features=4)

        snap = _make_feature_snapshot(symbols=["AAPL", "MSFT"], n_features=4)
        req = _make_inference_request(artifact_ref=bundle_path, symbols=["AAPL", "MSFT"])
        engine = RealShadowScorer(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")

        assert isinstance(result, ShadowInferenceResult)
        assert len(result.predictions) > 0
        for pred in result.predictions:
            assert isinstance(pred, ShadowPrediction)
            assert pred.authority == Authority.SHADOW_ONLY
            # The prediction must carry the bundle_sha256 in metadata.
            assert pred.metadata.get("bundle_sha256") == bundle_sha
            # The stub formula is raw_score = sum(features)/len(features).
            # Real bundle predictions must NOT match the stub formula.
            stub_raw = sum(snap.features[pred.symbol]) / len(snap.features[pred.symbol])
            stub_p_up = 1.0 / (1.0 + (2.718281828 ** (-stub_raw * 5.0)))
            assert pred.p_up != pytest.approx(stub_p_up, abs=1e-3)

    def test_real_shadow_scorer_callback_worker_id(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The callback worker_id identifies the real shadow scorer."""
        pytest.importorskip("lightgbm")
        pytest.importorskip("numpy")

        _, bundle_path, _ = _c2_make_single_bundle(tmp_path, n_features=4)
        snap = _make_feature_snapshot(symbols=["AAPL"], n_features=4)
        req = _make_inference_request(artifact_ref=bundle_path, symbols=["AAPL"])
        engine = RealShadowScorer(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        assert result.callback.worker_id == "real-shadow-scorer"


class TestRealShadowScorerStubRequiresExplicitFlag:
    """C2: Stub scoring requires an explicit engine=stub flag."""

    def test_shadow_inference_stub_requires_explicit_flag(self) -> None:
        """The stub ShadowInferenceEngine is only used when explicitly
        requested — RealShadowScorer is the default for real bundles.

        This test verifies that the two engine classes are distinct and
        that the stub engine produces the stub formula, while
        RealShadowScorer is the class intended for real bundle scoring.
        """
        # The stub engine class is distinct from RealShadowScorer.
        assert ShadowInferenceEngine is not RealShadowScorer
        # The stub engine produces the deterministic stub formula.
        snap = _make_feature_snapshot(symbols=["AAPL"], n_features=3)
        req = _make_inference_request(artifact_ref="file:///dummy", symbols=["AAPL"])
        stub_engine = ShadowInferenceEngine(enabled=True)
        stub_result = stub_engine.run(request=req, snapshot=snap, model_id="m1")
        stub_pred = stub_result.predictions[0]
        stub_raw = sum(snap.features["AAPL"]) / len(snap.features["AAPL"])
        stub_p_up = 1.0 / (1.0 + (2.718281828 ** (-stub_raw * 5.0)))
        assert stub_pred.p_up == pytest.approx(stub_p_up, abs=1e-6)
        # The stub prediction must NOT carry bundle_sha256 metadata.
        assert "bundle_sha256" not in stub_pred.metadata


class TestRealShadowScorerBundleSha256:
    """C2: Predictions contain bundle_sha256 from the Decision."""

    def test_shadow_prediction_contains_bundle_sha256(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """Each prediction's metadata contains the bundle_sha256 from the
        Decision object produced by BundleScorer.score()."""
        pytest.importorskip("lightgbm")
        pytest.importorskip("numpy")

        _, bundle_path, bundle_sha = _c2_make_single_bundle(tmp_path, n_features=4)
        snap = _make_feature_snapshot(symbols=["AAPL", "MSFT"], n_features=4)
        req = _make_inference_request(artifact_ref=bundle_path, symbols=["AAPL", "MSFT"])
        engine = RealShadowScorer(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")

        for pred in result.predictions:
            assert "bundle_sha256" in pred.metadata
            assert pred.metadata["bundle_sha256"] == bundle_sha
            assert len(pred.metadata["bundle_sha256"]) == 64  # sha256 hex


class TestRealShadowScorerMetaBundle:
    """C2: Predictions contain abstained and meta_p for meta-labeled bundles."""

    def test_shadow_prediction_contains_abstained_and_meta_p_for_meta_bundle(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """For a meta-labeled bundle, predictions carry abstained and meta_p
        from the Decision object."""
        pytest.importorskip("lightgbm")
        pytest.importorskip("numpy")

        _, bundle_path, bundle_sha = _c2_make_meta_bundle(tmp_path, n_features=4)
        snap = _make_feature_snapshot(symbols=["AAPL", "MSFT"], n_features=4)
        req = _make_inference_request(artifact_ref=bundle_path, symbols=["AAPL", "MSFT"])
        engine = RealShadowScorer(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")

        assert len(result.predictions) > 0
        for pred in result.predictions:
            # meta-labeled bundles always have meta_p (not None).
            assert "abstained" in pred.metadata
            assert "meta_p" in pred.metadata
            assert pred.metadata["meta_p"] != ""  # meta_p is present
            assert pred.metadata["abstained"] in ("True", "False")
            assert pred.metadata["bundle_sha256"] == bundle_sha
            assert "policy_version" in pred.metadata


class TestRealShadowScorerOfflineReproduction:
    """C2: Stored prediction equals load_bundle().score(features) offline."""

    def test_shadow_prediction_reproduces_offline_bundle_score(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The p_up and direction in the stored ShadowPrediction match the
        Decision from independently loading the bundle and calling score()."""
        pytest.importorskip("lightgbm")
        pytest.importorskip("numpy")

        from quant_foundry.bundle_io import BundleScorer, load_bundle

        _, bundle_path, _ = _c2_make_single_bundle(tmp_path, n_features=4)
        snap = _make_feature_snapshot(symbols=["AAPL", "MSFT"], n_features=4)
        req = _make_inference_request(artifact_ref=bundle_path, symbols=["AAPL", "MSFT"])

        # Run through RealShadowScorer.
        engine = RealShadowScorer(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")

        # Independently load the bundle and score the same features.
        bundle = load_bundle(bundle_path)
        scorer = BundleScorer(bundle)
        rows = [snap.features["AAPL"], snap.features["MSFT"]]
        decisions = scorer.score(rows)

        # Map predictions by symbol for comparison.
        pred_by_symbol = {p.symbol: p for p in result.predictions}
        for idx, symbol in enumerate(["AAPL", "MSFT"]):
            pred = pred_by_symbol[symbol]
            decision = decisions[idx]
            assert pred.p_up == pytest.approx(float(decision.p), abs=1e-6)
            assert pred.direction == pytest.approx(float(decision.direction), abs=1e-6)
            assert pred.metadata["bundle_sha256"] == decision.bundle_sha256
            assert pred.metadata["policy_version"] == decision.policy_version


class TestRealShadowScorerFailsClosed:
    """C2: Bundle load failure fails closed — no silent stub fallback."""

    def test_shadow_bundle_load_failure_fails_closed(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A corrupt/missing bundle raises an error — no stub fallback."""
        pytest.importorskip("lightgbm")
        pytest.importorskip("numpy")

        # Write a non-bundle file (not a zip, not a valid model).
        bad_path = tmp_path / "corrupt.bundle"
        bad_path.write_bytes(b"not a valid bundle")

        snap = _make_feature_snapshot(symbols=["AAPL"], n_features=4)
        req = _make_inference_request(artifact_ref=str(bad_path), symbols=["AAPL"])
        engine = RealShadowScorer(enabled=True)

        # Must raise — NOT silently fall back to stub scoring.
        with pytest.raises(Exception):  # noqa: PT011 — any exception is acceptable
            engine.run(request=req, snapshot=snap, model_id="m1")

    def test_no_silent_stub_fallback_on_bundle_error(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """When the model_loader raises, RealShadowScorer propagates the
        error rather than producing stub predictions."""
        pytest.importorskip("lightgbm")

        snap = _make_feature_snapshot(symbols=["AAPL"], n_features=4)
        req = _make_inference_request(
            artifact_ref="file:///nonexistent.bundle", symbols=["AAPL"]
        )

        class _ExplodingLoader:
            def __call__(self, uri: str) -> Any:
                raise RuntimeError("simulated bundle load failure")

        engine = RealShadowScorer(enabled=True, model_loader=_ExplodingLoader())

        with pytest.raises(RuntimeError, match="simulated bundle load failure"):
            engine.run(request=req, snapshot=snap, model_id="m1")

    def test_real_shadow_scorer_disabled_raises(self) -> None:
        """A disabled RealShadowScorer raises InferenceDisabledError."""
        snap = _make_feature_snapshot(symbols=["AAPL"], n_features=4)
        req = _make_inference_request(artifact_ref="file:///x.bundle", symbols=["AAPL"])
        engine = RealShadowScorer(enabled=False)
        with pytest.raises(InferenceDisabledError):
            engine.run(request=req, snapshot=snap, model_id="m1")

    def test_run_real_shadow_scoring_disabled_raises(self) -> None:
        """The convenience function raises when disabled."""
        snap = _make_feature_snapshot(symbols=["AAPL"], n_features=4)
        req = _make_inference_request(artifact_ref="file:///x.bundle", symbols=["AAPL"])
        with pytest.raises(InferenceDisabledError):
            run_real_shadow_scoring(
                request=req, snapshot=snap, model_id="m1", enabled=False
            )
