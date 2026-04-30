from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_impact_model.workbench import WorkbenchState


def _write_dataset(path: Path) -> None:
    rows = [
        {
            "event_id": "hist-1",
            "available_at_ns": 100,
            "source": "reuters",
            "headline": "Acme receives FDA approval for device",
            "symbols": ["ACME"],
            "event_type": "regulatory",
            "market_regime": "risk_on",
            "abnormal_returns": {"5m": 0.030, "30m": 0.050},
            "volatility_impact": 0.20,
            "volume_impact": 0.70,
        },
        {
            "event_id": "hist-2",
            "available_at_ns": 200,
            "source": "benzinga",
            "headline": "Acme wins regulator approval for product",
            "symbols": ["ACME"],
            "event_type": "regulatory",
            "market_regime": "risk_on",
            "abnormal_returns": {"5m": 0.026, "30m": 0.043},
            "volatility_impact": 0.18,
            "volume_impact": 0.62,
        },
        {
            "event_id": "hist-3",
            "available_at_ns": 300,
            "source": "reuters",
            "headline": "Acme receives FDA lawsuit over device approval",
            "symbols": ["ACME"],
            "event_type": "litigation",
            "market_regime": "risk_on",
            "abnormal_returns": {"5m": -0.030, "30m": -0.044},
            "volatility_impact": 0.25,
            "volume_impact": 0.75,
        },
        {
            "event_id": "hist-4",
            "available_at_ns": 400,
            "source": "newsapi",
            "headline": "Acme faces lawsuit after product approval",
            "symbols": ["ACME"],
            "event_type": "litigation",
            "market_regime": "risk_on",
            "abnormal_returns": {"5m": -0.026, "30m": -0.038},
            "volatility_impact": 0.21,
            "volume_impact": 0.58,
        },
    ]
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_workbench_loads_dataset_profile(tmp_path: Path) -> None:
    dataset = tmp_path / "historical_outcomes.jsonl"
    _write_dataset(dataset)
    state = WorkbenchState()

    profile = state.load_dataset(dataset)

    assert profile["path"] == str(dataset)
    assert profile["event_count"] == 4
    assert profile["horizons"] == ["5m", "30m"]
    assert profile["sources"] == {"benzinga": 1, "newsapi": 1, "reuters": 2}
    assert profile["event_types"] == {"litigation": 2, "regulatory": 2}
    assert profile["time_range_ns"] == {"start": 100, "end": 400}


def test_workbench_optimizes_loaded_dataset(tmp_path: Path) -> None:
    dataset = tmp_path / "historical_outcomes.jsonl"
    _write_dataset(dataset)
    state = WorkbenchState()
    state.load_dataset(dataset)

    result = state.optimize(
        horizon="5m",
        mode="walk-forward",
        min_train_events=2,
        top_k=5,
    )

    assert result["mode"] == "walk-forward"
    assert result["horizon"] == "5m"
    assert result["n_predictions"] == 2
    assert result["metrics"]["mae"] >= 0
    assert result["weights"]["event_type"] >= result["weights"]["text"]
    assert state.optimized_weights is not None


def test_workbench_predicts_from_loaded_dataset_and_optimized_weights(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "historical_outcomes.jsonl"
    _write_dataset(dataset)
    state = WorkbenchState()
    state.load_dataset(dataset)
    state.optimize(
        horizon="5m",
        mode="walk-forward",
        min_train_events=2,
        top_k=5,
    )

    prediction = state.predict(
        event={
            "event_id": "live-1",
            "available_at_ns": 500,
            "source": "benzinga",
            "headline": "Acme receives FDA approval",
            "body": "Regulator clears commercial launch.",
            "symbols": ["ACME"],
            "event_type": "regulatory",
        },
        context={"symbol": "ACME", "market_regime": "risk_on"},
        horizons=("5m", "30m"),
        top_k=3,
    )

    assert prediction["event_id"] == "live-1"
    assert prediction["symbol"] == "ACME"
    assert set(prediction["horizons"]) == {"5m", "30m"}
    assert prediction["horizons"]["5m"]["expected_return"] > 0
    assert prediction["similar_events"][0]["event_type"] == "regulatory"
