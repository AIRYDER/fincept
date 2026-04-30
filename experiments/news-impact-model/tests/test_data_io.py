from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_impact_model.data import (
    load_historical_outcomes,
    write_historical_outcomes_jsonl,
)
from news_impact_model.schema import HistoricalOutcome


def test_load_historical_outcomes_from_jsonl_normalizes_symbols_and_metadata(
    tmp_path: Path,
) -> None:
    path = tmp_path / "outcomes.jsonl"
    path.write_text(
        json.dumps(
            {
                "event_id": "evt-1",
                "available_at_ns": 1_700_000_000_000_000_000,
                "source": "reuters",
                "headline": "Acme receives FDA approval",
                "body": "Commercial launch can begin.",
                "symbols": ["ACME", "BETA"],
                "event_type": "regulatory",
                "market_regime": "risk_on",
                "abnormal_returns": {"5m": 0.018, "30m": 0.031},
                "volatility_impact": 0.22,
                "volume_impact": 0.71,
                "metadata": {"provider_event_id": "abc-123"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    outcomes = load_historical_outcomes(path)

    assert len(outcomes) == 1
    assert outcomes[0].symbols == ("ACME", "BETA")
    assert outcomes[0].abnormal_returns == {"5m": 0.018, "30m": 0.031}
    assert outcomes[0].metadata == {"provider_event_id": "abc-123"}


def test_load_historical_outcomes_from_csv_accepts_return_columns(
    tmp_path: Path,
) -> None:
    path = tmp_path / "outcomes.csv"
    path.write_text(
        "\n".join(
            [
                "event_id,available_at_ns,source,headline,body,symbols,event_type,"
                "market_regime,return_5m,abnormal_return_30m,volatility_impact,"
                "volume_impact,metadata_provider",
                "evt-1,100,reuters,Approval lifts Acme,,ACME|BETA,regulatory,"
                "risk_on,0.02,0.04,0.2,0.8,reuters-feed",
            ]
        ),
        encoding="utf-8",
    )

    outcomes = load_historical_outcomes(path)

    assert outcomes[0].event_id == "evt-1"
    assert outcomes[0].symbols == ("ACME", "BETA")
    assert outcomes[0].abnormal_returns == {"5m": 0.02, "30m": 0.04}
    assert outcomes[0].metadata == {"provider": "reuters-feed"}


def test_write_historical_outcomes_jsonl_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "roundtrip.jsonl"
    original = HistoricalOutcome(
        event_id="evt-1",
        available_at_ns=100,
        source="benzinga",
        headline="Acme announces surprise contract",
        body="The contract expands annual revenue guidance.",
        symbols=("ACME",),
        event_type="contract",
        market_regime="risk_neutral",
        abnormal_returns={"5m": 0.012},
        volatility_impact=0.11,
        volume_impact=0.33,
        metadata={"raw_id": "42"},
    )

    write_historical_outcomes_jsonl(path, [original])

    loaded = load_historical_outcomes(path)
    assert loaded == [original]


def test_optimize_weights_script_reads_dataset_and_emits_json(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.jsonl"
    rows = [
        {
            "event_id": "hist-1",
            "available_at_ns": 100,
            "source": "reuters",
            "headline": "Acme receives FDA approval for device",
            "symbols": ["ACME"],
            "event_type": "regulatory",
            "market_regime": "risk_on",
            "abnormal_returns": {"5m": 0.030},
        },
        {
            "event_id": "hist-2",
            "available_at_ns": 200,
            "source": "reuters",
            "headline": "Acme wins regulator approval for product",
            "symbols": ["ACME"],
            "event_type": "regulatory",
            "market_regime": "risk_on",
            "abnormal_returns": {"5m": 0.026},
        },
        {
            "event_id": "hist-3",
            "available_at_ns": 300,
            "source": "reuters",
            "headline": "Acme receives FDA lawsuit over device approval",
            "symbols": ["ACME"],
            "event_type": "litigation",
            "market_regime": "risk_on",
            "abnormal_returns": {"5m": -0.030},
        },
        {
            "event_id": "hist-4",
            "available_at_ns": 400,
            "source": "reuters",
            "headline": "Acme faces lawsuit after product approval",
            "symbols": ["ACME"],
            "event_type": "litigation",
            "market_regime": "risk_on",
            "abnormal_returns": {"5m": -0.026},
        },
    ]
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "optimize_weights.py"),
            str(path),
            "--horizon",
            "5m",
            "--mode",
            "walk-forward",
            "--min-train-events",
            "2",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["mode"] == "walk-forward"
    assert payload["horizon"] == "5m"
    assert payload["n_predictions"] == 2
    assert payload["weights"]["event_type"] >= payload["weights"]["text"]
