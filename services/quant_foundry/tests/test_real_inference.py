"""
Tests for the real model-loading shadow inference engine (RealInferenceEngine).

These tests mirror the invariants enforced by ``ShadowInferenceEngine`` but
exercise the real model-loading path. ML deps (onnxruntime / lightgbm) are
imported lazily inside the engine, so the import-only tests run without them;
tests that actually load a model use ``pytest.importorskip``.

Safety invariants verified:
- ``Authority.SHADOW_ONLY`` always — no trading predictions.
- Disabled by default — ``InferenceDisabledError`` if not enabled.
- Abstain on low feature availability (skip unavailable symbols).
- Latency tracking on every prediction + the result.
- Signed callback envelope with ``result_type="inference_batch"``.
- Backward compat: ``ShadowInferenceEngine`` (stub) still works.
- ONNX + LightGBM model loading via ``ModelLoader``.
"""

from __future__ import annotations

import pickle
from typing import Any

import pytest
from quant_foundry.real_inference import (
    ModelLoader,
    RealInferenceEngine,
    load_bundle_scorer,
    run_real_inference,
)
from quant_foundry.schemas import (
    Authority,
    RunPodCallbackEnvelope,
    RunPodInferenceRequest,
    ShadowPrediction,
)
from quant_foundry.shadow_inference import (
    FeatureSnapshot,
    InferenceDisabledError,
    ShadowInferenceEngine,
    ShadowInferenceResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_feature_snapshot(
    symbols: list[str] | None = None,
    n_features: int = 5,
) -> FeatureSnapshot:
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
    artifact_ref: str = "file:///dummy.onnx",
    symbols: list[str] | None = None,
) -> RunPodInferenceRequest:
    if symbols is None:
        symbols = ["AAPL", "MSFT"]
    return RunPodInferenceRequest(
        job_id="job-1",
        artifact_ref=artifact_ref,
        symbols=symbols,
        horizons_ns=[3600_000_000_000],
    )


class _StubScorer:
    """A fake scorer returning a fixed scalar per row."""

    def __init__(self, outputs: list[float] | None = None) -> None:
        self.outputs = outputs if outputs is not None else [0.8, -0.4]
        self.calls: list[list[list[float]]] = []

    def predict(self, features: list[list[float]]) -> list[Any]:
        self.calls.append(features)
        return list(self.outputs[: len(features)])


def _stub_loader(scorer: _StubScorer) -> Any:
    """Return a model_loader callable that ignores the URI and returns scorer."""

    def _loader(uri: str) -> Any:
        return scorer

    return _loader


# ---------------------------------------------------------------------------
# Import / lazy-import
# ===========================================================================


class TestLazyImports:
    """The module is importable without onnxruntime/lightgbm installed."""

    def test_module_imports_without_ml_deps(self) -> None:
        import quant_foundry.real_inference as mod

        assert hasattr(mod, "RealInferenceEngine")
        assert hasattr(mod, "ModelLoader")
        assert hasattr(mod, "run_real_inference")

    def test_model_loader_imports_without_ml_deps(self) -> None:
        loader = ModelLoader()
        assert loader.fetcher is None

    def test_engine_constructs_without_ml_deps(self) -> None:
        engine = RealInferenceEngine(enabled=False)
        assert engine.enabled is False
        assert engine.model_loader is None


# ---------------------------------------------------------------------------
# Disabled-by-default fail-safe
# ===========================================================================


class TestDisabledByDefault:
    """A disabled engine raises InferenceDisabledError."""

    def test_disabled_raises(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = RealInferenceEngine(enabled=False)
        with pytest.raises(InferenceDisabledError):
            engine.run(request=req, snapshot=snap, model_id="m1")

    def test_default_disabled_raises(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = RealInferenceEngine()  # default enabled=False
        with pytest.raises(InferenceDisabledError):
            engine.run(request=req, snapshot=snap, model_id="m1")

    def test_run_real_inference_disabled_raises(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        with pytest.raises(InferenceDisabledError):
            run_real_inference(
                request=req,
                snapshot=snap,
                model_id="m1",
                enabled=False,
            )


# ---------------------------------------------------------------------------
# Real predictions with a mock model loader
# ===========================================================================


class TestRealPredictions:
    """Real predictions are produced when enabled with a mock model loader."""

    def test_returns_shadow_inference_result(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        scorer = _StubScorer(outputs=[0.8, -0.4])
        engine = RealInferenceEngine(
            enabled=True,
            model_loader=_stub_loader(scorer),
        )
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        assert isinstance(result, ShadowInferenceResult)
        assert len(result.predictions) > 0

    def test_predictions_are_shadow_predictions(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        scorer = _StubScorer(outputs=[0.8, -0.4])
        engine = RealInferenceEngine(
            enabled=True,
            model_loader=_stub_loader(scorer),
        )
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        for pred in result.predictions:
            assert isinstance(pred, ShadowPrediction)

    def test_predictions_have_correct_model_id(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        scorer = _StubScorer(outputs=[0.8, -0.4])
        engine = RealInferenceEngine(
            enabled=True,
            model_loader=_stub_loader(scorer),
        )
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        for pred in result.predictions:
            assert pred.model_id == "m1"

    def test_predictions_have_correct_symbols(self) -> None:
        snap = _make_feature_snapshot(symbols=["AAPL", "MSFT"])
        req = _make_inference_request(symbols=["AAPL", "MSFT"])
        scorer = _StubScorer(outputs=[0.8, -0.4])
        engine = RealInferenceEngine(
            enabled=True,
            model_loader=_stub_loader(scorer),
        )
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        pred_symbols = {p.symbol for p in result.predictions}
        assert pred_symbols == {"AAPL", "MSFT"}

    def test_direction_in_valid_range(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        scorer = _StubScorer(outputs=[10.0, -10.0])  # clamped
        engine = RealInferenceEngine(
            enabled=True,
            model_loader=_stub_loader(scorer),
        )
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        for pred in result.predictions:
            assert -1.0 <= pred.direction <= 1.0

    def test_confidence_in_valid_range(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        scorer = _StubScorer(outputs=[10.0, -10.0])
        engine = RealInferenceEngine(
            enabled=True,
            model_loader=_stub_loader(scorer),
        )
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        for pred in result.predictions:
            assert 0.0 <= pred.confidence <= 1.0

    def test_p_up_in_valid_range(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        scorer = _StubScorer(outputs=[10.0, -10.0])
        engine = RealInferenceEngine(
            enabled=True,
            model_loader=_stub_loader(scorer),
        )
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        for pred in result.predictions:
            assert pred.p_up is not None
            assert 0.0 <= pred.p_up <= 1.0

    def test_model_loader_is_called_with_artifact_ref(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request(artifact_ref="file:///models/m.onnx")
        scorer = _StubScorer(outputs=[0.8, -0.4])
        seen_uris: list[str] = []

        def _loader(uri: str) -> Any:
            seen_uris.append(uri)
            return scorer

        engine = RealInferenceEngine(enabled=True, model_loader=_loader)
        engine.run(request=req, snapshot=snap, model_id="m1")
        assert seen_uris == ["file:///models/m.onnx"]

    def test_scorer_receives_feature_rows(self) -> None:
        snap = _make_feature_snapshot(symbols=["AAPL", "MSFT"], n_features=3)
        req = _make_inference_request(symbols=["AAPL", "MSFT"])
        scorer = _StubScorer(outputs=[0.8, -0.4])
        engine = RealInferenceEngine(
            enabled=True,
            model_loader=_stub_loader(scorer),
        )
        engine.run(request=req, snapshot=snap, model_id="m1")
        assert len(scorer.calls) == 1
        rows = scorer.calls[0]
        assert len(rows) == 2
        assert rows[0] == [0.0, 0.1, 0.2]

    def test_binary_proba_output_centered(self) -> None:
        """A 2-element probability vector is mapped to p(class=1) - 0.5."""
        snap = _make_feature_snapshot(symbols=["AAPL"], n_features=2)
        req = _make_inference_request(symbols=["AAPL"])

        class _BinaryScorer:
            def predict(self, features: list[list[float]]) -> list[Any]:
                return [[0.2, 0.9]]  # p(class=1)=0.9 -> score 0.4

        engine = RealInferenceEngine(enabled=True, model_loader=_stub_loader(_BinaryScorer()))  # type: ignore[arg-type]
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        assert len(result.predictions) == 1
        assert result.predictions[0].direction == pytest.approx(0.4, abs=1e-6)


# ---------------------------------------------------------------------------
# Authority.SHADOW_ONLY enforcement
# ===========================================================================


class TestShadowOnlyAuthority:
    """Authority.SHADOW_ONLY is always enforced."""

    def test_authority_is_shadow_only(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        scorer = _StubScorer(outputs=[0.8, -0.4])
        engine = RealInferenceEngine(
            enabled=True,
            model_loader=_stub_loader(scorer),
        )
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        for pred in result.predictions:
            assert pred.authority == Authority.SHADOW_ONLY

    def test_predictions_have_no_order_fields(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        scorer = _StubScorer(outputs=[0.8, -0.4])
        engine = RealInferenceEngine(
            enabled=True,
            model_loader=_stub_loader(scorer),
        )
        result = engine.run(request=req, snapshot=snap, model_id="m1")
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
        for pred in result.predictions:
            d = pred.model_dump()
            assert not any(k in d for k in order_keys)


# ---------------------------------------------------------------------------
# Abstain on low availability
# ===========================================================================


class TestAbstainOnLowAvailability:
    """Symbols with availability=False are skipped (abstain)."""

    def test_low_availability_symbol_skipped(self) -> None:
        snap = FeatureSnapshot(
            symbols=["AAPL", "MSFT"],
            features={"AAPL": [0.1, 0.2], "MSFT": [0.3, 0.4]},
            availability={"AAPL": True, "MSFT": False},
            ts_event=1000,
            freshness_ns=500,
        )
        req = _make_inference_request(symbols=["AAPL", "MSFT"])
        scorer = _StubScorer(outputs=[0.8, -0.4])
        engine = RealInferenceEngine(
            enabled=True,
            model_loader=_stub_loader(scorer),
        )
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        pred_symbols = {p.symbol for p in result.predictions}
        assert pred_symbols == {"AAPL"}
        # Scorer only saw the available row.
        assert len(scorer.calls[0]) == 1

    def test_missing_symbol_skipped(self) -> None:
        snap = _make_feature_snapshot(symbols=["AAPL"])
        req = _make_inference_request(symbols=["AAPL", "MSFT"])
        scorer = _StubScorer(outputs=[0.8])
        engine = RealInferenceEngine(
            enabled=True,
            model_loader=_stub_loader(scorer),
        )
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        pred_symbols = {p.symbol for p in result.predictions}
        assert pred_symbols == {"AAPL"}

    def test_empty_snapshot_produces_no_predictions(self) -> None:
        snap = FeatureSnapshot(
            symbols=[],
            features={},
            availability={},
            ts_event=1000,
            freshness_ns=500,
        )
        req = _make_inference_request()
        scorer = _StubScorer(outputs=[])
        engine = RealInferenceEngine(
            enabled=True,
            model_loader=_stub_loader(scorer),
        )
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        assert len(result.predictions) == 0


# ---------------------------------------------------------------------------
# Latency tracking
# ===========================================================================


class TestLatencyTracking:
    """Latency is tracked on the result and each prediction."""

    def test_result_has_latency(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        scorer = _StubScorer(outputs=[0.8, -0.4])
        engine = RealInferenceEngine(
            enabled=True,
            model_loader=_stub_loader(scorer),
        )
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        assert result.latency_ms >= 0.0

    def test_predictions_have_latency(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        scorer = _StubScorer(outputs=[0.8, -0.4])
        engine = RealInferenceEngine(
            enabled=True,
            model_loader=_stub_loader(scorer),
        )
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        for pred in result.predictions:
            assert pred.latency_ms is not None
            assert pred.latency_ms >= 0.0


# ---------------------------------------------------------------------------
# Signed callback envelope
# ===========================================================================


class TestSignedCallback:
    """The result includes a signed callback envelope."""

    def test_callback_envelope_present(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        scorer = _StubScorer(outputs=[0.8, -0.4])
        engine = RealInferenceEngine(
            enabled=True,
            model_loader=_stub_loader(scorer),
        )
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        assert isinstance(result.callback, RunPodCallbackEnvelope)
        assert result.callback.job_id == req.job_id
        assert result.callback.result_type == "inference_batch"

    def test_callback_payload_contains_predictions(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        scorer = _StubScorer(outputs=[0.8, -0.4])
        engine = RealInferenceEngine(
            enabled=True,
            model_loader=_stub_loader(scorer),
        )
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        assert "predictions" in result.callback.payload
        assert len(result.callback.payload["predictions"]) > 0
        assert result.callback.payload["model_id"] == "m1"
        assert result.callback.payload["artifact_ref"] == req.artifact_ref

    def test_callback_worker_id_is_real_engine(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        scorer = _StubScorer(outputs=[0.8, -0.4])
        engine = RealInferenceEngine(
            enabled=True,
            model_loader=_stub_loader(scorer),
        )
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        assert result.callback.worker_id == "real-inference-engine"


# ---------------------------------------------------------------------------
# Backward compat: stub engine still works
# ===========================================================================


class TestStubEngineBackwardCompat:
    """The original ShadowInferenceEngine (stub) still works unchanged."""

    def test_stub_engine_returns_predictions(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        assert isinstance(result, ShadowInferenceResult)
        assert len(result.predictions) > 0
        for pred in result.predictions:
            assert pred.authority == Authority.SHADOW_ONLY

    def test_stub_engine_disabled_raises(self) -> None:
        snap = _make_feature_snapshot()
        req = _make_inference_request()
        engine = ShadowInferenceEngine(enabled=False)
        with pytest.raises(InferenceDisabledError):
            engine.run(request=req, snapshot=snap, model_id="m1")


# ---------------------------------------------------------------------------
# ModelLoader — ONNX
# ===========================================================================


class TestModelLoaderOnnx:
    """ModelLoader loads ONNX artifacts (skipped if onnxruntime unavailable)."""

    def test_load_onnx_file(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        onnx = pytest.importorskip("onnxruntime")
        np = pytest.importorskip("numpy")

        # Build a tiny synthetic ONNX model: y = sum(x).
        try:
            import onnx
            from onnx import TensorProto, helper
        except ImportError:
            pytest.skip("onnx package not available to build a synthetic model")

        X = helper.make_tensor_value_info("X", TensorProto.FLOAT, [None, 2])
        Y = helper.make_tensor_value_info("Y", TensorProto.FLOAT, [None, 1])

        # ReduceSum over axis=1, keepdims=1.
        node = helper.make_node(
            "ReduceSum",
            inputs=["X"],
            outputs=["Y"],
            axes=[1],
            keepdims=1,
        )
        graph = helper.make_graph([node], "sum", [X], [Y])
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
        model.ir_version = 8
        path = tmp_path / "model.onnx"
        onnx.save(model, str(path))

        loader = ModelLoader()
        scorer = loader.load(f"file://{path}")
        out = scorer.predict([[1.0, 2.0], [3.0, 4.0]])
        arr = np.asarray(out, dtype=np.float32).reshape(-1)
        assert arr[0] == pytest.approx(3.0, abs=1e-4)
        assert arr[1] == pytest.approx(7.0, abs=1e-4)

    def test_onnx_engine_end_to_end(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        pytest.importorskip("onnxruntime")
        pytest.importorskip("numpy")
        try:
            import onnx
            from onnx import TensorProto, helper
        except ImportError:
            pytest.skip("onnx package not available")

        X = helper.make_tensor_value_info("X", TensorProto.FLOAT, [None, 3])
        Y = helper.make_tensor_value_info("Y", TensorProto.FLOAT, [None, 1])
        node = helper.make_node(
            "ReduceSum",
            inputs=["X"],
            outputs=["Y"],
            axes=[1],
            keepdims=1,
        )
        graph = helper.make_graph([node], "sum", [X], [Y])
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
        model.ir_version = 8
        path = tmp_path / "model.onnx"
        onnx.save(model, str(path))

        snap = _make_feature_snapshot(symbols=["AAPL"], n_features=3)
        req = _make_inference_request(artifact_ref=f"file://{path}", symbols=["AAPL"])
        engine = RealInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        assert len(result.predictions) == 1
        # features = [0.0, 0.1, 0.2] -> sum = 0.3 -> clamped to 0.3
        assert result.predictions[0].direction == pytest.approx(0.3, abs=1e-3)


# ---------------------------------------------------------------------------
# ModelLoader — LightGBM
# ===========================================================================


class TestModelLoaderLightgbm:
    """ModelLoader loads LightGBM artifacts (skipped if lightgbm unavailable)."""

    def test_load_lightgbm_txt(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        lgb = pytest.importorskip("lightgbm")
        np = pytest.importorskip("numpy")

        rng = np.random.default_rng(0)
        X = rng.normal(size=(50, 3)).astype(np.float32)
        y = (X @ np.array([1.0, -2.0, 0.5])).astype(np.float32)
        dtrain = lgb.Dataset(X, label=y)
        booster = lgb.train(
            {"objective": "regression", "verbose": -1, "num_leaves": 4},
            dtrain,
            num_boost_round=5,
        )
        path = tmp_path / "model.txt"
        booster.save_model(str(path))

        loader = ModelLoader()
        scorer = loader.load(f"file://{path}")
        out = scorer.predict([[0.0, 0.1, 0.2]])
        assert len(out) == 1

    def test_load_lightgbm_pickle(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        lgb = pytest.importorskip("lightgbm")
        np = pytest.importorskip("numpy")

        rng = np.random.default_rng(1)
        X = rng.normal(size=(30, 2)).astype(np.float32)
        y = (X @ np.array([1.0, -1.0])).astype(np.float32)
        dtrain = lgb.Dataset(X, label=y)
        booster = lgb.train(
            {"objective": "regression", "verbose": -1, "num_leaves": 4},
            dtrain,
            num_boost_round=3,
        )
        path = tmp_path / "model.pkl"
        with open(path, "wb") as fh:
            pickle.dump(booster, fh)

        loader = ModelLoader()
        scorer = loader.load(f"file://{path}")
        out = scorer.predict([[0.1, 0.2]])
        assert len(out) == 1

    def test_lightgbm_engine_end_to_end(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        lgb = pytest.importorskip("lightgbm")
        np = pytest.importorskip("numpy")

        rng = np.random.default_rng(0)
        X = rng.normal(size=(50, 5)).astype(np.float32)
        y = (X @ np.array([1.0, -2.0, 0.5, 0.3, -0.7])).astype(np.float32)
        dtrain = lgb.Dataset(X, label=y)
        booster = lgb.train(
            {"objective": "regression", "verbose": -1, "num_leaves": 4},
            dtrain,
            num_boost_round=5,
        )
        path = tmp_path / "model.txt"
        booster.save_model(str(path))

        snap = _make_feature_snapshot(symbols=["AAPL", "MSFT"], n_features=5)
        req = _make_inference_request(artifact_ref=f"file://{path}", symbols=["AAPL", "MSFT"])
        engine = RealInferenceEngine(enabled=True)
        result = engine.run(request=req, snapshot=snap, model_id="m1")
        assert len(result.predictions) == 2
        for pred in result.predictions:
            assert pred.authority == Authority.SHADOW_ONLY
            assert -1.0 <= pred.direction <= 1.0


# ---------------------------------------------------------------------------
# ModelLoader — URI resolution / errors
# ===========================================================================


class TestModelLoaderUriResolution:
    """ModelLoader resolves URIs and rejects unsupported schemes/extensions."""

    def test_file_uri_resolved(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        # Use a lightgbm model if available, otherwise just check resolution
        # by asserting the loader raises a known error for a bad extension.
        loader = ModelLoader()
        path = tmp_path / "model.unknown"
        path.write_text("noop")
        with pytest.raises(RuntimeError, match="unsupported model artifact"):
            loader.load(f"file://{path}")

    def test_s3_uri_without_fetcher_raises(self) -> None:
        loader = ModelLoader()
        with pytest.raises(RuntimeError, match="s3:// URIs require"):
            loader.load("s3://bucket/key.onnx")

    def test_s3_uri_with_fetcher(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        lgb = pytest.importorskip("lightgbm")
        np = pytest.importorskip("numpy")

        rng = np.random.default_rng(2)
        X = rng.normal(size=(20, 2)).astype(np.float32)
        y = (X @ np.array([1.0, -1.0])).astype(np.float32)
        booster = lgb.train(
            {"objective": "regression", "verbose": -1, "num_leaves": 4},
            lgb.Dataset(X, label=y),
            num_boost_round=2,
        )
        path = tmp_path / "model.txt"
        booster.save_model(str(path))

        def _fetcher(uri: str) -> str:
            assert uri.startswith("s3://")
            return str(path)

        loader = ModelLoader(fetcher=_fetcher)
        scorer = loader.load("s3://bucket/model.txt")
        out = scorer.predict([[0.1, 0.2]])
        assert len(out) == 1


# ---------------------------------------------------------------------------
# C2: load_bundle_scorer — explicit bundle loading for shadow scoring
# ===========================================================================


class TestLoadBundleScorer:
    """C2: load_bundle_scorer loads a C1 bundle and returns a BundleScorer
    with .score() for full Decision objects."""

    def test_load_bundle_scorer_returns_scorer_with_score_method(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """load_bundle_scorer returns an object with .score() (BundleScorer)."""
        lgb = pytest.importorskip("lightgbm")
        np = pytest.importorskip("numpy")
        from quant_foundry.bundle_io import write_bundle

        rng = np.random.RandomState(42)
        X = rng.randn(60, 3)
        y = (X[:, 0] > 0).astype(float)
        model = lgb.train(
            {"objective": "binary", "verbosity": -1, "num_leaves": 8, "seed": 42},
            lgb.Dataset(X, label=y),
            num_boost_round=10,
        )
        bundle_bytes = write_bundle(
            primary_model=model,
            feature_names=["f1", "f2", "f3"],
            feature_schema_hash="hash-f",
            label_schema_hash="hash-l",
            model_family="gbm",
        )
        path = tmp_path / "test.bundle"
        path.write_bytes(bundle_bytes)

        scorer = load_bundle_scorer(str(path))
        # BundleScorer exposes .score() -> list[Decision].
        assert hasattr(scorer, "score")
        decisions = scorer.score([[0.1, 0.2, 0.3]])
        assert len(decisions) == 1
        # Decision carries bundle_sha256.
        assert len(decisions[0].bundle_sha256) == 64

    def test_load_bundle_scorer_fails_closed_on_corrupt_bundle(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """load_bundle_scorer raises on a corrupt bundle (no stub fallback)."""
        bad_path = tmp_path / "corrupt.bundle"
        bad_path.write_bytes(b"not a valid bundle")
        with pytest.raises(Exception):  # noqa: PT011
            load_bundle_scorer(str(bad_path))

    def test_model_loader_load_bundle_returns_scorer_with_score(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """ModelLoader.load() on a C1 bundle returns a BundleScorer with
        .score() — the C1 work already added this; this test confirms the
        contract for C2 shadow use."""
        lgb = pytest.importorskip("lightgbm")
        np = pytest.importorskip("numpy")
        from quant_foundry.bundle_io import write_bundle

        rng = np.random.RandomState(7)
        X = rng.randn(40, 2)
        y = (X[:, 0] > 0).astype(float)
        model = lgb.train(
            {"objective": "binary", "verbosity": -1, "num_leaves": 4, "seed": 7},
            lgb.Dataset(X, label=y),
            num_boost_round=5,
        )
        bundle_bytes = write_bundle(
            primary_model=model,
            feature_names=["a", "b"],
            feature_schema_hash="hf",
            label_schema_hash="hl",
            model_family="gbm",
        )
        path = tmp_path / "m.bundle"
        path.write_bytes(bundle_bytes)

        loader = ModelLoader()
        scorer = loader.load(str(path))
        assert hasattr(scorer, "score")
        decisions = scorer.score([[0.1, 0.2]])
        assert len(decisions) == 1
        assert hasattr(decisions[0], "bundle_sha256")
        assert hasattr(decisions[0], "abstained")
        assert hasattr(decisions[0], "policy_version")
