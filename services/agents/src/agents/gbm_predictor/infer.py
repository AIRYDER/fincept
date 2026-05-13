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
from pydantic import BaseModel
from redis.asyncio import Redis

from features.store import OnlineStore
from fincept_core.clock import now_ns
from fincept_core.config import get_settings
from fincept_core.logging import get_logger
from fincept_core.schemas import Prediction

from agents.base import Agent
from agents.gbm_predictor.features import FEATURES, load_live

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
                row = await load_live(
                    self._store,
                    symbol,
                    feature_names=self._features,
                    freq=self._freq,
                    allow_compat_defaults=True,
                )
                if row is None:
                    continue
                prediction = self._predict(symbol, row)
                yield prediction
            await asyncio.sleep(self._cadence_s)

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
