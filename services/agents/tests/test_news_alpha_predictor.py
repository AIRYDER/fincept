from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest
from fincept_core.schemas import FeatureFrame

from agents.news_alpha_predictor.features import DEFAULT_FEATURES, extract_sentiment_row
from agents.news_alpha_predictor.infer import NewsAlphaPredictor
from agents.news_alpha_predictor.main import resolve_model_dir


class FakeModel:
    def __init__(self, value: float) -> None:
        self.value = value

    def predict(self, data: Any) -> list[float]:
        assert len(data[0]) == 2
        return [self.value]


def _frame(**values: float | None) -> FeatureFrame:
    payload = {name: 0.1 for name in DEFAULT_FEATURES}
    payload.update(values)
    return FeatureFrame(
        symbol="NVDA",
        ts_event=1_000,
        freq="sentiment",
        values=payload,
        tags={"latest_event_category": "earnings"},
    )


def test_extract_sentiment_row_requires_sentiment_freq() -> None:
    frame = FeatureFrame(symbol="NVDA", ts_event=1, freq="1m", values={})
    assert extract_sentiment_row(frame) is None


def test_extract_sentiment_row_projects_features() -> None:
    row = extract_sentiment_row(_frame(), feature_names=["sentiment_30m"])
    assert row == {"sentiment_30m": pytest.approx(0.1)}


def test_predict_frame_maps_probability_to_prediction(tmp_path: pathlib.Path) -> None:
    predictor = NewsAlphaPredictor(
        model_dir=tmp_path,
        model=FakeModel(0.75),
        feature_names=["sentiment_30m", "sentiment_30m_article_count"],
        horizon_ns=30,
    )

    prediction = predictor.predict_frame(_frame())

    assert prediction is not None
    assert prediction.agent_id == "news_alpha_predictor.v1"
    assert prediction.symbol == "NVDA"
    assert prediction.horizon_ns == 30
    assert prediction.direction == pytest.approx(0.5)
    assert prediction.confidence == pytest.approx(0.5)
    assert prediction.calibration_tag == "news_alpha.v1"


def test_resolve_model_dir_prefers_active_pointer(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = tmp_path / "models"
    active = models / "active"
    active.mkdir(parents=True)
    (active / "news_alpha_predictor.v1.json").write_text(
        json.dumps({"model_name": "news_alpha_predictor_candidate"})
    )
    monkeypatch.setenv("MODELS_DIR", str(models))
    monkeypatch.delenv("ACTIVE_MODELS_DIR", raising=False)
    monkeypatch.setenv("NEWS_ALPHA_MODEL_DIR", str(tmp_path / "fallback"))

    assert resolve_model_dir() == models / "news_alpha_predictor_candidate"


def test_resolve_model_dir_falls_back_to_env(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback = tmp_path / "fallback"
    monkeypatch.setenv("NEWS_ALPHA_MODEL_DIR", str(fallback))
    monkeypatch.setenv("MODELS_DIR", str(tmp_path / "models"))

    assert resolve_model_dir() == fallback
