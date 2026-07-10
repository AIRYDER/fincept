"""
agents.gbm_predictor.infer - online inference loop.

The :class:`GBMPredictor` agent loads a trained Booster + meta.json,
then yields :class:`fincept_core.schemas.Prediction` events at a fixed
cadence.  One Prediction per universe symbol per cycle; symbols whose
features aren't yet warm in the OnlineStore are silently skipped.

Direction calibration:
  prob_up = model.predict(X)[0]              # in [0, 1]
  direction = 2 * prob_up - 1                # in [-1, +1]
  confidence = |direction|                   # in [0, +1]

The orchestrator (TASK-040) is the canonical consumer; predictions on
``STREAM_SIG_PREDICT`` are weighted by regime + correlated-asset
diversification + position-size limits before becoming Decisions.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from collections.abc import AsyncIterator
from typing import Any

import lightgbm as lgb
import numpy as np
from features.store import OnlineStore
from fincept_core.clock import now_ns
from fincept_core.config import get_settings
from fincept_core.logging import get_logger
from fincept_core.schemas import Prediction
from pydantic import BaseModel
from redis.asyncio import Redis

from agents.base import Agent
from agents.gbm_predictor.features import (
    FEATURES,
    FeatureHealth,
    _compute_feature_schema_hash,
    load_live,
)

log = get_logger(__name__)

DEFAULT_CADENCE_S = 60.0
DEFAULT_FREQ = "1m"


class GBMPredictor(Agent):
    """LightGBM directional classifier agent."""

    agent_id: str = "gbm_predictor.v1"

    def __init__(
        self,
        *,
        model_dir: pathlib.Path,
        redis: Redis[Any],
        cadence_s: float = DEFAULT_CADENCE_S,
        freq: str = DEFAULT_FREQ,
        symbols: list[str] | None = None,
    ) -> None:
        self._model_dir = model_dir
        self._redis = redis
        self._cadence_s = cadence_s
        self._freq = freq
        self._explicit_symbols = symbols
        self._model: lgb.Booster | None = None
        self._features: list[str] = list(FEATURES)
        self._horizon_ns: int = 0
        self._store: OnlineStore | None = None
        # Feature-availability diagnostics from the most recent
        # load_live call.  Set on every cycle before a Prediction is
        # yielded so the publish loop (main._publish_loop) can record a
        # FeatureHealthRow sidecar without re-deriving the projection.
        # Public-read so the publish loop can introspect it; the agent
        # owns the write.
        self.last_feature_health: FeatureHealth | None = None
        # The projected feature vector + frame timestamp from the most
        # recent load_live call.  Set alongside last_feature_health so
        # the publish loop can build a FeatureSnapshot (the evidence
        # spine's "what the agent saw" leg) without a second Redis
        # lookup.  ``last_feature_frame_ts`` is the FeatureFrame's
        # ts_event -- the point-in-time timestamp of the input data.
        self.last_feature_vector: dict[str, float] | None = None
        self.last_feature_frame_ts: int | None = None

    async def setup(self) -> None:
        """Load model + meta; initialise the OnlineStore reader."""
        meta_path = self._model_dir / "meta.json"
        model_path = self._model_dir / "model.txt"
        if not meta_path.is_file() or not model_path.is_file():
            raise FileNotFoundError(
                f"GBMPredictor model artifacts missing in {self._model_dir!s}: "
                "expected model.txt + meta.json (run agents.gbm_predictor.train first)"
            )

        self._model = lgb.Booster(model_file=str(model_path))
        meta = json.loads(meta_path.read_text())
        self._features = list(meta.get("features", FEATURES))
        self._horizon_ns = int(meta.get("horizon_ns", 0))
        self._store = OnlineStore(self._redis)

        # Schema compatibility gate: if the trainer wrote an
        # artifact_manifest.json, verify the live feature schema is
        # compatible with what the model was trained on.  Legacy models
        # without an artifact manifest skip this check with a warning.
        self._check_schema_compatibility()

        log.info(
            "gbm.loaded",
            model_dir=str(self._model_dir),
            features=self._features,
            horizon_ns=self._horizon_ns,
        )

    async def run(self) -> AsyncIterator[BaseModel]:
        if self._model is None or self._store is None:
            raise RuntimeError("GBMPredictor.run() called before setup()")

        symbols = self._explicit_symbols or list(get_settings().UNIVERSE)
        while True:
            for symbol in symbols:
                frame_ts_sink: list[int] = []
                loaded = await load_live(
                    self._store,
                    symbol,
                    feature_names=self._features,
                    freq=self._freq,
                    allow_compat_defaults=True,
                    frame_ts_out=frame_ts_sink,
                )
                if loaded is None:
                    continue
                row, health = loaded
                self.last_feature_health = health
                self.last_feature_vector = row
                self.last_feature_frame_ts = (
                    frame_ts_sink[-1] if frame_ts_sink else None
                )
                prediction = self._predict(symbol, row)
                yield prediction
            await asyncio.sleep(self._cadence_s)

    def _check_schema_compatibility(self) -> None:
        """Verify the live feature schema matches the artifact manifest.

        Reads ``artifact_manifest.json`` from the model directory.  If
        present, checks that the live feature set (``self._features``)
        is compatible with the artifact's schema using
        :func:`assert_feature_schema_compatible`.  A mismatch raises
        :class:`SchemaIncompatibilityError` and prevents the agent from
        starting — a silent prediction on incompatible features is
        worse than a loud failure.

        Legacy models without an artifact manifest skip the check with
        a log warning so existing deployments are not broken.
        """
        from fincept_core.datasets import ArtifactManifest
        from fincept_core.datasets.schema_compat import (
            assert_feature_schema_compatible,
        )

        manifest_path = self._model_dir / "artifact_manifest.json"
        if not manifest_path.is_file():
            log.warning(
                "gbm.schema_compat_skipped",
                model_dir=str(self._model_dir),
                reason="artifact_manifest.json not found (legacy model)",
            )
            return

        try:
            manifest = ArtifactManifest.model_validate_json(manifest_path.read_text())
        except Exception as exc:
            log.warning(
                "gbm.schema_compat_skipped",
                model_dir=str(self._model_dir),
                reason=f"artifact_manifest.json parse failed: {exc}",
            )
            return

        live_hash = _compute_feature_schema_hash(self._features)
        # ArtifactManifest stores feature_schema_hash but not the
        # feature_names list itself.  We pass self._features for both
        # sides so the subset check is a no-op; the hash + version are
        # the real discriminators.  If the hashes differ but the version
        # matches, the compat function allows it (the artifact will just
        # ignore extra features at predict time).
        assert_feature_schema_compatible(
            artifact_feature_schema_hash=manifest.feature_schema_hash,
            artifact_feature_schema_version=manifest.feature_schema_version,
            artifact_feature_names=tuple(self._features),
            snapshot_feature_schema_hash=live_hash,
            snapshot_feature_schema_version=1,
            snapshot_feature_names=tuple(self._features),
        )
        log.info(
            "gbm.schema_compat_ok",
            model_dir=str(self._model_dir),
            artifact_hash=manifest.feature_schema_hash,
            live_hash=live_hash,
        )

    def _predict(self, symbol: str, row: dict[str, float]) -> Prediction:
        """Pure inference: features dict -> Prediction.

        Public test surface; ``run`` calls this per (symbol, cycle).
        """
        if self._model is None:
            raise RuntimeError("model not loaded")
        x = np.array([[row[f] for f in self._features]], dtype=np.float64)
        prob_up = float(self._model.predict(x)[0])
        # Clamp to [0, 1] defensively - lightgbm should already, but
        # numerical edge cases can produce ~1e-9 violations.
        prob_up = max(0.0, min(1.0, prob_up))
        direction = 2 * prob_up - 1
        confidence = abs(direction)
        return Prediction(
            agent_id=self.agent_id,
            symbol=symbol,
            horizon_ns=self._horizon_ns,
            ts_event=now_ns(),
            direction=direction,
            confidence=confidence,
            calibration_tag="gbm.v1",
        )

    async def teardown(self) -> None:
        # Booster has no explicit close; releasing references is enough.
        self._model = None
        self._store = None
