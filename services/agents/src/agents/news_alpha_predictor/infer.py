from __future__ import annotations

import json
import pathlib
from typing import Any, Protocol

import lightgbm as lgb
import numpy as np
from fincept_core.clock import now_ns
from fincept_core.schemas import FeatureFrame, Prediction

from agents.news_alpha_predictor.features import DEFAULT_FEATURES, extract_sentiment_row

AGENT_ID = "news_alpha_predictor.v1"
DEFAULT_HORIZON_NS = 30 * 60 * 1_000_000_000


class _Model(Protocol):
    def predict(self, data: Any) -> Any: ...


class NewsAlphaPredictor:
    agent_id = AGENT_ID

    def __init__(
        self,
        *,
        model_dir: pathlib.Path,
        model: _Model | None = None,
        feature_names: list[str] | None = None,
        horizon_ns: int | None = None,
    ) -> None:
        self._model_dir = model_dir
        self._model = model
        self._features = feature_names or list(DEFAULT_FEATURES)
        self._horizon_ns = horizon_ns or DEFAULT_HORIZON_NS

    def load(self) -> None:
        meta_path = self._model_dir / "meta.json"
        model_path = self._model_dir / "model.txt"
        if not meta_path.is_file() or not model_path.is_file():
            raise FileNotFoundError(
                f"NewsAlphaPredictor model artifacts missing in {self._model_dir!s}: "
                "expected model.txt + meta.json"
            )
        meta = json.loads(meta_path.read_text())
        self._features = list(meta.get("features", DEFAULT_FEATURES))
        self._horizon_ns = int(meta.get("horizon_ns", DEFAULT_HORIZON_NS))
        self._model = lgb.Booster(model_file=str(model_path))

    def predict_frame(self, frame: FeatureFrame) -> Prediction | None:
        if self._model is None:
            raise RuntimeError("NewsAlphaPredictor used before load()")
        row = extract_sentiment_row(frame, feature_names=self._features)
        if row is None:
            return None
        x = np.array([[row[name] for name in self._features]], dtype=np.float64)
        prob_up = float(self._model.predict(x)[0])
        prob_up = max(0.0, min(1.0, prob_up))
        direction = 2 * prob_up - 1
        confidence = abs(direction)
        return Prediction(
            agent_id=self.agent_id,
            symbol=frame.symbol,
            horizon_ns=self._horizon_ns,
            ts_event=now_ns(),
            direction=direction,
            confidence=confidence,
            calibration_tag="news_alpha.v1",
        )
