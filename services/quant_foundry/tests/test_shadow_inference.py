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

File-disjoint from Builder 2's runpod/quant-foundry-training/ (different
subdirectory). Imports ShadowPrediction from schemas.py (read-only) and
ArtifactRecord from my artifacts.py (TASK-0503).
"""

from __future__ import annotations

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
    ShadowInferenceEngine,
    ShadowInferenceResult,
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
