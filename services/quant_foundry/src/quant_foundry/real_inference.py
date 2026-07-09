"""
quant_foundry.real_inference — real model-loading shadow inference engine.

A drop-in replacement for ``ShadowInferenceEngine`` that loads actual model
artifacts (ONNX or LightGBM) and produces real predictions instead of the
deterministic stub (``sum(features)/len(features)``) used by the MVP.

Key invariants (identical to ``ShadowInferenceEngine``):
- **Shadow-only authority.** All predictions have ``authority: shadow_only``.
  No order/trading fields are ever produced.
- **Disabled by default.** Raises ``InferenceDisabledError`` unless enabled.
- **Fails safely on invalid input.** Missing symbols, empty snapshots, and
  low feature availability produce abstaining predictions (skip), not crashes.
- **Latency + feature availability.** Each prediction includes latency_ms and
  feature_availability.
- **Signed callback.** The result includes a ``RunPodCallbackEnvelope`` with
  ``result_type="inference_batch"``.

Lazy imports: ``onnxruntime`` and ``lightgbm`` are imported INSIDE the methods
that need them, so this module is importable without ML deps installed.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from typing import Any, Protocol, cast

from quant_foundry.schemas import (
    Authority,
    RunPodCallbackEnvelope,
    RunPodInferenceRequest,
    ShadowPrediction,
)
from quant_foundry.shadow_inference import (
    FeatureSnapshot,
    InferenceDisabledError,
    ShadowInferenceResult,
)

try:
    # fincept_core.logging pulls in structlog + config; it may not be present
    # in the minimal RunPod inference container. Fall back to stdlib logging
    # so importing this module never crashes the worker on startup.
    from fincept_core.logging import get_logger
except ImportError:  # pragma: no cover - fincept-core present in-workspace
    import logging as _logging

    def get_logger(name: str) -> Any:  # type: ignore[misc]
        return _logging.getLogger(name)


try:
    from fincept_core.storage import StorageBackend, get_storage_backend
except ImportError:  # pragma: no cover - fincept-core always present in-workspace
    StorageBackend = None  # type: ignore[assignment,misc]
    get_storage_backend = None  # type: ignore[assignment]

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Model loader
# ---------------------------------------------------------------------------


class _Scorer(Protocol):
    """Minimal protocol for a loaded model that can score feature vectors."""

    def predict(self, features: list[list[float]]) -> list[Any]: ...


class ModelLoader:
    """Default model loader supporting ONNX and LightGBM artifacts.

    Loads a model artifact from a URI based on its file extension:
    - ``.onnx`` → ``onnxruntime.InferenceSession``
    - ``.pkl`` or ``.txt`` → ``lightgbm.Booster``

    Both ML deps are imported lazily inside ``load`` so that this class (and
    the whole module) is importable without onnxruntime/lightgbm installed.

    Currently supports ``file://`` URIs (local read-only cache). ``s3://``
    URIs are accepted and routed through the injected ``fetcher`` callable
    (which must return a local path or bytes); if no ``fetcher`` is provided
    an ``InferenceDisabledError``-free ``RuntimeError`` is raised.
    """

    def __init__(
        self,
        fetcher: Callable[[str], str] | None = None,
        storage_backend: Any = None,
    ) -> None:
        self.fetcher = fetcher
        self.storage_backend = storage_backend

    def load(self, uri: str) -> _Scorer:
        """Load a model artifact from a URI and return a scorer.

        Supports:
        - ModelBundle v1 (zip archive) → ``BundleScorer`` (C1).
        - Legacy bare LightGBM pickle → ``_LgbScorer``.
        - ONNX → ``_OnnxScorer``.
        - LightGBM text model → ``_LgbScorer``.

        Raises ``RuntimeError`` if the URI scheme is unsupported or the
        extension is unknown. Raises ``ImportError`` if the required ML dep
        is not installed.
        """
        path = self._resolve_uri(uri)
        # C1: detect ModelBundle v1 (zip archive) by magic number, not
        # extension — the bundle is a zip containing bundle_manifest.json
        # + member pickles. This handles .bundle, .pkl, and any extension
        # the writer may have used.
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                magic = fh.read(4)
            if magic == b"PK\x03\x04":
                return self._load_bundle(path)
        ext = os.path.splitext(path)[1].lower()
        if ext == ".onnx":
            return self._load_onnx(path)
        if ext in (".pkl", ".txt", ".bundle"):
            # .pkl may be a legacy bare pickle or a bundle (if the magic
            # check above didn't catch it, e.g. empty file). Try bundle
            # first for .bundle, fall back to legacy LightGBM for .pkl/.txt.
            if ext == ".bundle":
                return self._load_bundle(path)
            return self._load_lightgbm(path)
        raise RuntimeError(f"unsupported model artifact extension: {ext!r} (uri={uri!r})")

    @staticmethod
    def _load_bundle(path: str) -> _Scorer:
        """Load a ModelBundle v1 from a file path and return a BundleScorer.

        The BundleScorer implements the _Scorer protocol (.predict) and
        also exposes .score() for full Decision objects with meta_p and
        abstention.
        """
        from quant_foundry.bundle_io import BundleScorer, load_bundle

        bundle = load_bundle(path)
        return BundleScorer(bundle)

    def _resolve_uri(self, uri: str) -> str:
        """Resolve a URI to a local filesystem path.

        For ``s3://`` URIs, the configured ``storage_backend`` (or the factory
        singleton, when it is S3-capable) is used to download the artifact to a
        temp file. The legacy ``fetcher`` callable is still honored for backward
        compat. For ``file://`` URIs and bare paths, behavior is unchanged.
        """
        if uri.startswith("file://"):
            return uri[len("file://") :]
        if uri.startswith("s3://"):
            if self.storage_backend is not None:
                return str(self.storage_backend.download_to_temp(uri))
            if get_storage_backend is not None:
                try:
                    backend = get_storage_backend()
                except Exception as exc:
                    raise RuntimeError(
                        f"no storage backend available for s3 model artifact: {exc}"
                    ) from exc
                try:
                    return backend.download_to_temp(uri)
                except Exception as exc:
                    log.debug("model_artifact.s3_download_failed", error=str(exc))
            if self.fetcher is not None:
                return self.fetcher(uri)
            raise RuntimeError("s3:// URIs require an injected storage_backend or fetcher callable")
        # Treat bare paths as local filesystem paths.
        return uri

    @staticmethod
    def _load_onnx(path: str) -> _Scorer:
        """Load an ONNX model into an onnxruntime InferenceSession."""
        import onnxruntime as ort  # lazy import

        session = ort.InferenceSession(path)

        class _OnnxScorer:
            def __init__(self, sess: Any) -> None:
                self.sess = sess
                self.input_name = sess.get_inputs()[0].name

            def predict(self, features: list[list[float]]) -> list[Any]:
                import numpy as np  # lazy import

                arr = np.asarray(features, dtype=np.float32)
                if arr.ndim == 1:
                    arr = arr.reshape(1, -1)
                outputs = self.sess.run(None, {self.input_name: arr})
                return list(outputs[0])

        return _OnnxScorer(session)

    @staticmethod
    def _load_lightgbm(path: str) -> _Scorer:
        """Load a LightGBM model from a text/pickle file."""
        import lightgbm as lgb  # lazy import

        if path.endswith(".pkl"):
            import pickle

            with open(path, "rb") as fh:
                # The trainer produces this trusted model artifact; never load user bytes here.
                booster = pickle.load(fh)  # noqa: S301
            if not isinstance(booster, lgb.Booster):
                raise RuntimeError(
                    f"pickle did not contain a lightgbm.Booster (got {type(booster)!r})"
                )
        else:
            booster = lgb.Booster(model_file=path)

        class _LgbScorer:
            def __init__(self, bst: Any) -> None:
                self.bst = bst

            def predict(self, features: list[list[float]]) -> list[Any]:
                import numpy as np  # lazy import

                arr = np.asarray(features, dtype=np.float32)
                if arr.ndim == 1:
                    arr = arr.reshape(1, -1)
                return list(self.bst.predict(arr))

        return _LgbScorer(booster)


# ---------------------------------------------------------------------------
# The real inference engine
# ===========================================================================


def _default_model_loader(storage_backend: Any = None) -> ModelLoader:
    return ModelLoader(storage_backend=storage_backend)


def _sigmoid(x: float) -> float:
    return float(1.0 / (1.0 + (2.718281828 ** (-x))))


class RealInferenceEngine:
    """Real model-loading shadow inference engine.

    Same interface as ``ShadowInferenceEngine`` but loads an actual model
    artifact (via the injected ``model_loader`` callable) and runs real
    predictions on the ``FeatureSnapshot`` feature vectors.

    Args:
    - ``enabled``: fail-safe toggle. Defaults to ``False`` (disabled).
    - ``model_loader``: a callable ``(uri: str) -> scorer`` that loads a
      model artifact. Defaults to ``ModelLoader().load``.

    The scorer must expose a ``predict(features: list[list[float]]) -> list``
      method returning per-row raw model outputs (logits, probabilities, or
      regression scores). The engine maps these to ``direction``,
      ``confidence``, and ``p_up`` on ``ShadowPrediction``.
    """

    def __init__(
        self,
        enabled: bool = False,
        model_loader: Callable[[str], Any] | None = None,
        storage_backend: Any = None,
    ) -> None:
        self.enabled = enabled
        self.model_loader = model_loader
        self.storage_backend = storage_backend

    def run(
        self,
        request: RunPodInferenceRequest,
        snapshot: FeatureSnapshot,
        model_id: str,
    ) -> ShadowInferenceResult:
        """Run real shadow inference on a feature snapshot.

        Args:
        - ``request``: the RunPodInferenceRequest (job_id, artifact_ref, symbols, horizons).
        - ``snapshot``: the feature snapshot to score.
        - ``model_id``: the model ID to attach to predictions.

        Returns a ``ShadowInferenceResult`` with predictions + callback + latency.

        Raises ``InferenceDisabledError`` if the engine is disabled.
        """
        if not self.enabled:
            raise InferenceDisabledError(
                "real shadow inference is disabled "
                "(QUANT_FOUNDRY_MODE != runpod_shadow); "
                "no predictions produced — fail-safe"
            )

        start_ns = time.time_ns()

        # Collect available symbols + their feature rows in request order.
        scored_symbols: list[str] = []
        rows: list[list[float]] = []
        availability_map: dict[str, bool] = {}
        for symbol in request.symbols:
            if symbol not in snapshot.features:
                continue
            available = snapshot.availability.get(symbol, False)
            if not available:
                continue
            features = snapshot.features.get(symbol, [])
            if not features:
                continue
            scored_symbols.append(symbol)
            rows.append(list(features))
            availability_map[symbol] = available

        # Run the model if there is anything to score.
        raw_outputs: list[Any] = []
        if scored_symbols:
            loader = self.model_loader
            if loader is None:
                loader = _default_model_loader(self.storage_backend).load
            scorer = loader(request.artifact_ref)
            raw_outputs = list(scorer.predict(rows))

        predictions: list[ShadowPrediction] = []
        for idx, symbol in enumerate(scored_symbols):
            raw = raw_outputs[idx] if idx < len(raw_outputs) else 0.0
            score = _coerce_score(raw)
            direction = max(-1.0, min(1.0, float(score)))
            confidence = min(1.0, abs(score) * 0.5 + 0.25)
            p_up = _sigmoid(score * 5.0)
            available = availability_map[symbol]
            for horizon_ns in request.horizons_ns:
                pred = ShadowPrediction(
                    prediction_id=str(uuid.uuid4()),
                    model_id=model_id,
                    symbol=symbol,
                    ts_event=snapshot.ts_event,
                    horizon_ns=horizon_ns,
                    direction=direction,
                    confidence=confidence,
                    authority=Authority.SHADOW_ONLY,
                    p_up=p_up,
                    feature_availability={symbol: available},
                    latency_ms=0.0,
                )
                predictions.append(pred)

        elapsed_ns = time.time_ns() - start_ns
        latency_ms = elapsed_ns / 1e6

        predictions = [
            p.model_copy(update={"latency_ms": latency_ms / max(len(predictions), 1)})
            for p in predictions
        ]

        callback = RunPodCallbackEnvelope(
            job_id=request.job_id,
            worker_id="real-inference-engine",
            result_type="inference_batch",
            payload={
                "predictions": [p.model_dump() for p in predictions],
                "model_id": model_id,
                "n_predictions": len(predictions),
                "artifact_ref": request.artifact_ref,
            },
        )

        return ShadowInferenceResult(
            predictions=predictions,
            callback=callback,
            latency_ms=latency_ms,
        )


def _coerce_score(raw: Any) -> float:
    """Coerce a raw model output into a single scalar score.

    Handles common model output shapes:
    - scalar number → that number
    - 1-element sequence → first element
    - 2-element sequence (binary proba) → p(class=1) - 0.5 (centered)
    - longer sequence → first element
    """
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        seq = list(cast(Any, raw))
    except TypeError:
        return 0.0
    if not seq:
        return 0.0
    if len(seq) == 2:
        return float(seq[1]) - 0.5
    return float(seq[0])


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def run_real_inference(
    request: RunPodInferenceRequest,
    snapshot: FeatureSnapshot,
    model_id: str,
    enabled: bool = False,
    model_loader: Callable[[str], Any] | None = None,
    storage_backend: Any = None,
) -> ShadowInferenceResult:
    """Run real shadow inference on a feature snapshot.

    Convenience entry point mirroring ``run_shadow_inference``. Creates a
    ``RealInferenceEngine`` and runs it. Raises ``InferenceDisabledError`` if
    ``enabled`` is False.
    """
    engine = RealInferenceEngine(
        enabled=enabled,
        model_loader=model_loader,
        storage_backend=storage_backend,
    )
    return engine.run(request=request, snapshot=snapshot, model_id=model_id)
