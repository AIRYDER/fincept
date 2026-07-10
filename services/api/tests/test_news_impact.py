from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_news_impact_status_loads_sample_dataset(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.get("/news-impact/status", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["dataset_loaded"] is True
    assert body["profile"]["event_count"] > 0
    assert body["mode"] == "experimental_demo"


async def test_news_impact_predict_scores_event(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post(
        "/news-impact/predict",
        headers=auth_headers,
        json={
            "event": {
                "event_id": "test-live-1",
                "source": "benzinga",
                "headline": "Amazon raises AWS outlook after AI infrastructure demand accelerates",
                "body": "Management says demand remains strong.",
                "symbols": ["AMZN"],
                "event_type": "guidance",
            },
            "context": {
                "symbol": "AMZN",
                "market_regime": "risk_on",
                "relative_volume": 1.8,
            },
            "horizons": ["5m", "30m", "1h"],
            "top_k": 5,
        },
    )
    assert response.status_code == 200
    body = response.json()
    prediction = body["prediction"]
    assert prediction["symbol"] == "AMZN"
    assert prediction["model_version"].startswith("news-impact-analog")
    assert set(prediction["horizons"]) == {"5m", "30m", "1h"}
    assert "confidence" in prediction
    assert isinstance(prediction["similar_events"], list)


async def test_news_impact_optimize_returns_weights(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post(
        "/news-impact/optimize",
        headers=auth_headers,
        json={
            "horizon": "5m",
            "mode": "leave-one-out",
            "top_k": 3,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["optimization"]["horizon"] == "5m"
    assert body["optimization"]["weights"]
