"""
quant_foundry.shadow_inference — RunPod shadow inference engine (TASK-0601).

Runs candidate model predictions and returns shadow-only prediction batches.
Shadow inference is a **measurement lane, not a trading lane** — predictions
are settled against realized outcomes but never reach ``sig.predict``.

Key invariants:
- **Shadow-only authority.** All predictions have ``authority: shadow_only``.
  No order/trading fields are ever produced. The ``ShadowPrediction`` schema
  enforces this with ``extra='forbid'``.
- **Disabled by default.** Inference can be disabled without breaking Fincept
  (``InferenceDisabledError``).
- **Fails safely on invalid input.** Missing symbols, empty snapshots, and
  low feature availability produce abstaining predictions (low confidence or
  no predictions), not crashes.
- **Latency + feature availability.** Each prediction includes latency_ms and
  feature_availability so the operator can diagnose stale or incomplete data.
- **Signed callback.** The result includes a ``RunPodCallbackEnvelope`` for
  the signed callback to Fincept.

C2 (real shadow scorer): ``RealShadowScorer`` loads a C1 bundle via
``ModelLoader.load(artifact_ref)`` and calls ``BundleScorer.score(features)``
to produce full ``Decision`` objects. The Decision fields (``bundle_sha256``,
``abstained``, ``meta_p``, ``policy_version``) are carried on the
``ShadowPrediction.metadata`` dict so the schema stays ``extra='forbid'``.
**No silent fallback to stub scoring on bundle error — fail closed.**

File-disjoint from Builder 2's ``runpod/quant-foundry-training/`` (different
subdirectory). Imports ``ShadowPrediction`` / ``RunPodInferenceRequest`` /
``RunPodCallbackEnvelope`` from ``schemas.py`` (read-only).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from quant_foundry.schemas import (
    Authority,
    RunPodCallbackEnvelope,
    RunPodInferenceRequest,
    ShadowPrediction,
)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InferenceDisabledError(RuntimeError):
    """Raised when inference is disabled (fail-safe — no predictions produced)."""


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class FeatureSnapshot(BaseModel):
    """A compact, point-in-time feature snapshot for shadow inference.

    Frozen + extra='forbid'. Carries per-symbol feature vectors, availability
    flags, a decision timestamp, and freshness metadata. If availability is
    too low, the engine abstains rather than predicting on incomplete data.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbols: list[str]
    features: dict[str, list[float]] = {}
    availability: dict[str, bool] = {}
    ts_event: int = 0
    freshness_ns: int = 0


class ShadowInferenceResult(BaseModel):
    """Result of a shadow inference run.

    Frozen + extra='forbid'. Carries the batch of ShadowPrediction objects,
    the signed RunPodCallbackEnvelope, and the overall latency.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    predictions: list[ShadowPrediction] = []
    callback: RunPodCallbackEnvelope
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for audit/persistence."""
        return {
            "predictions": [p.model_dump() for p in self.predictions],
            "callback": self.callback.model_dump(),
            "latency_ms": self.latency_ms,
        }


# ---------------------------------------------------------------------------
# The inference engine
# ===========================================================================


class ShadowInferenceEngine:
    """Shadow inference engine for candidate model predictions.

    Runs predictions on a feature snapshot and returns a batch of
    ``ShadowPrediction`` objects with ``authority: shadow_only``. The engine
    is disabled by default (fail-safe) and can be enabled via the
    ``enabled`` flag.

    The engine does NOT load actual model artifacts (that would require
    LightGBM/ONNX inference in the RunPod container). Instead, it produces
    deterministic stub predictions from the feature snapshot, suitable for
    testing the inference pipeline end-to-end. The RunPod handler
    (``runpod/quant-foundry-inference/handler.py``) wraps this engine and
    can inject a real model loader.
    """

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    def run(
        self,
        request: RunPodInferenceRequest,
        snapshot: FeatureSnapshot,
        model_id: str,
    ) -> ShadowInferenceResult:
        """Run shadow inference on a feature snapshot.

        Args:
        - ``request``: the RunPodInferenceRequest (job_id, artifact_ref, symbols, horizons).
        - ``snapshot``: the feature snapshot to score.
        - ``model_id``: the model ID to attach to predictions.

        Returns a ``ShadowInferenceResult`` with predictions + callback + latency.

        Raises ``InferenceDisabledError`` if the engine is disabled.
        """
        if not self.enabled:
            raise InferenceDisabledError(
                "shadow inference is disabled (QUANT_FOUNDRY_MODE != runpod_shadow); "
                "no predictions produced — fail-safe"
            )

        start_ns = time.time_ns()
        predictions: list[ShadowPrediction] = []

        for symbol in request.symbols:
            # Check if the symbol has features in the snapshot.
            if symbol not in snapshot.features:
                # Missing symbol — abstain (skip).
                continue

            # Check feature availability.
            available = snapshot.availability.get(symbol, False)
            if not available:
                # Low availability — abstain (skip).
                continue

            features = snapshot.features.get(symbol, [])
            if not features:
                continue

            # Produce a deterministic stub prediction from the features.
            # In a real deployment, this would call the loaded model.
            # For the MVP, we use a simple linear combination of features.
            raw_score = sum(features) / max(len(features), 1)
            direction = max(-1.0, min(1.0, raw_score * 2.0))
            confidence = min(1.0, abs(raw_score) + 0.3)
            p_up = 1.0 / (1.0 + (2.718281828 ** (-raw_score * 5.0)))

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
                    latency_ms=0.0,  # filled in below
                )
                predictions.append(pred)

        # Compute overall latency.
        elapsed_ns = time.time_ns() - start_ns
        latency_ms = elapsed_ns / 1e6

        # Attach latency to each prediction.
        predictions = [
            p.model_copy(update={"latency_ms": latency_ms / max(len(predictions), 1)})
            for p in predictions
        ]

        # Build the signed callback envelope.
        callback = RunPodCallbackEnvelope(
            job_id=request.job_id,
            worker_id="shadow-inference-engine",
            result_type="inference_batch",
            payload={
                "predictions": [p.model_dump() for p in predictions],
                "model_id": model_id,
                "n_predictions": len(predictions),
            },
        )

        return ShadowInferenceResult(
            predictions=predictions,
            callback=callback,
            latency_ms=latency_ms,
        )


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def run_shadow_inference(
    request: RunPodInferenceRequest,
    snapshot: FeatureSnapshot,
    model_id: str,
    enabled: bool = False,
) -> ShadowInferenceResult:
    """Run shadow inference on a feature snapshot.

    Convenience entry point for TASK-0601. Creates a ``ShadowInferenceEngine``
    and runs it. Raises ``InferenceDisabledError`` if ``enabled`` is False.
    """
    engine = ShadowInferenceEngine(enabled=enabled)
    return engine.run(request=request, snapshot=snapshot, model_id=model_id)


# ---------------------------------------------------------------------------
# C2: Real shadow scorer using C1 bundles
# ===========================================================================


def _default_bundle_loader(storage_backend: Any = None) -> Callable[[str], Any]:
    """Return a model_loader callable that loads a bundle via ModelLoader.

    Deferred import of ``quant_foundry.real_inference`` so this module stays
    importable without the real_inference dependency graph at import time.
    """
    from quant_foundry.real_inference import ModelLoader

    loader = ModelLoader(storage_backend=storage_backend)

    def _load(uri: str) -> Any:
        return loader.load(uri)

    return _load


class RealShadowScorer:
    """Real shadow scorer that uses C1 bundles via ``BundleScorer.score()``.

    Unlike ``ShadowInferenceEngine`` (deterministic stub) and
    ``RealInferenceEngine`` (raw ``predict()`` → manual mapping), this scorer
    loads a C1 ``ModelBundle`` through ``ModelLoader.load(artifact_ref)``,
    calls ``BundleScorer.score(features)`` to obtain full ``Decision``
    objects, and maps the Decision fields onto ``ShadowPrediction``.

    The bundle-derived fields (``bundle_sha256``, ``abstained``, ``meta_p``,
    ``policy_version``) are carried on ``ShadowPrediction.metadata`` because
    the ``ShadowPrediction`` schema is ``extra='forbid'`` and frozen (we do
    not modify the shared schema).

    Key invariants:
    - **Fail closed on bundle error.** If the bundle cannot be loaded or
      scored, the engine raises — it NEVER silently falls back to the stub
      scorer. This prevents a corrupt/missing bundle from producing
      un-attributed stub predictions that look like real model output.
    - **Shadow-only authority.** All predictions have ``authority:
      shadow_only``.
    - **Disabled by default.** Raises ``InferenceDisabledError`` unless
      enabled.
    - **Abstain on low availability.** Symbols with ``availability=False``
      or missing features are skipped (no prediction emitted).

    Args:
    - ``enabled``: fail-safe toggle. Defaults to ``False``.
    - ``model_loader``: a callable ``(uri: str) -> scorer`` that loads a
      model artifact and returns a ``BundleScorer`` (or any object exposing
      ``.score(features) -> list[Decision]``). Defaults to
      ``ModelLoader().load``.
    - ``storage_backend``: optional storage backend for s3:// URI resolution.
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
        """Run real bundle-based shadow inference.

        Args:
        - ``request``: the RunPodInferenceRequest (job_id, artifact_ref,
          symbols, horizons).
        - ``snapshot``: the feature snapshot to score.
        - ``model_id``: the model ID to attach to predictions.

        Returns a ``ShadowInferenceResult`` with predictions + callback +
        latency. Each prediction's ``metadata`` dict carries
        ``bundle_sha256``, ``abstained``, ``meta_p``, and ``policy_version``
        from the ``Decision``.

        Raises ``InferenceDisabledError`` if the engine is disabled.
        Raises on bundle load/score failure (fail closed — no stub fallback).
        """
        if not self.enabled:
            raise InferenceDisabledError(
                "real shadow scorer is disabled "
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

        # Load the bundle and score via BundleScorer.score().
        # Fail closed: any bundle load/score error propagates — NO stub
        # fallback. This is the core C2 invariant.
        decisions: list[Any] = []
        if scored_symbols:
            loader = self.model_loader
            if loader is None:
                loader = _default_bundle_loader(self.storage_backend)
            scorer = loader(request.artifact_ref)
            # BundleScorer exposes .score() -> list[Decision]. If the loaded
            # object only has .predict() (legacy ONNX/LightGBM scorer), we
            # cannot produce full Decision objects — fail closed rather than
            # silently degrading to raw predict().
            if not hasattr(scorer, "score"):
                raise RuntimeError(
                    f"loaded scorer for artifact_ref={request.artifact_ref!r} "
                    "does not expose .score() — RealShadowScorer requires a "
                    "BundleScorer (C1 bundle). Use RealInferenceEngine for "
                    "legacy ONNX/LightGBM artifacts."
                )
            decisions = list(scorer.score(rows))

        predictions: list[ShadowPrediction] = []
        for idx, symbol in enumerate(scored_symbols):
            decision = decisions[idx] if idx < len(decisions) else None
            available = availability_map[symbol]

            if decision is not None:
                # Map Decision → ShadowPrediction.
                p_up = float(decision.p)
                direction = float(decision.direction)
                # Confidence: high when not abstaining, reduced when abstaining.
                confidence = 0.3 if decision.abstained else min(1.0, abs(p_up - 0.5) * 2.0 + 0.5)
                meta_p_val = decision.meta_p
                metadata: dict[str, str] = {
                    "bundle_sha256": str(decision.bundle_sha256),
                    "abstained": str(decision.abstained),
                    "meta_p": "" if meta_p_val is None else str(meta_p_val),
                    "policy_version": str(decision.policy_version),
                }
            else:
                # No decision (shouldn't happen if rows non-empty, but
                # defensive): emit an abstaining prediction with no bundle
                # metadata.
                p_up = 0.5
                direction = 0.0
                confidence = 0.0
                metadata = {}

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
                    latency_ms=0.0,  # filled in below
                    metadata=metadata,
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
            worker_id="real-shadow-scorer",
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


def run_real_shadow_scoring(
    request: RunPodInferenceRequest,
    snapshot: FeatureSnapshot,
    model_id: str,
    enabled: bool = False,
    model_loader: Callable[[str], Any] | None = None,
    storage_backend: Any = None,
) -> ShadowInferenceResult:
    """Run real bundle-based shadow inference.

    Convenience entry point mirroring ``run_shadow_inference``. Creates a
    ``RealShadowScorer`` and runs it. Raises ``InferenceDisabledError`` if
    ``enabled`` is False. Raises on bundle load/score failure (fail closed).
    """
    engine = RealShadowScorer(
        enabled=enabled,
        model_loader=model_loader,
        storage_backend=storage_backend,
    )
    return engine.run(request=request, snapshot=snapshot, model_id=model_id)
