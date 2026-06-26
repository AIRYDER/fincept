from __future__ import annotations

import json
import pathlib
import time

from agents.news_alpha_predictor.evaluate import (
    CandidateGatePolicy,
    evaluate_candidate,
    write_report,
)


def _write_model(
    path: pathlib.Path, *, auc: float = 0.61, rows: int = 500, val_rows: int = 100
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "model.txt").write_text("model")
    (path / "meta.json").write_text(
        json.dumps(
            {
                "best_auc": auc,
                "rows": rows,
                "val_rows": val_rows,
                "trained_at": time.time(),
                "features": ["sentiment_30m"],
                "horizon": "30m",
                "horizon_ns": 30 * 60 * 1_000_000_000,
            }
        )
    )


def test_evaluate_candidate_approves_model_that_passes_policy(
    tmp_path: pathlib.Path,
) -> None:
    models = tmp_path / "models"
    candidate = models / "news_alpha_predictor_candidate"
    _write_model(candidate, auc=0.62, rows=500, val_rows=100)

    report = evaluate_candidate(
        candidate_dir=candidate,
        models_dir=models,
        policy=CandidateGatePolicy(min_auc=0.55, min_rows=200, min_val_rows=40),
    )

    assert report.approved is True
    assert report.reasons == []
    assert report.candidate_model_name == "news_alpha_predictor_candidate"
    assert (
        report.promotion_hint["shadow"]["path"]
        == "/models/news_alpha_predictor_candidate/shadow"
    )
    assert (
        report.promotion_hint["active"]["body"]["agent_id"] == "news_alpha_predictor.v1"
    )


def test_evaluate_candidate_rejects_low_auc_and_rows(tmp_path: pathlib.Path) -> None:
    models = tmp_path / "models"
    candidate = models / "news_alpha_predictor_candidate"
    _write_model(candidate, auc=0.50, rows=20, val_rows=5)

    report = evaluate_candidate(
        candidate_dir=candidate,
        models_dir=models,
        policy=CandidateGatePolicy(min_auc=0.55, min_rows=200, min_val_rows=40),
    )

    assert report.approved is False
    assert any("rows" in reason for reason in report.reasons)
    assert any("val_rows" in reason for reason in report.reasons)
    assert any("best_auc" in reason for reason in report.reasons)


def test_evaluate_candidate_compares_against_active_auc(tmp_path: pathlib.Path) -> None:
    models = tmp_path / "models"
    active_dir = models / "active"
    candidate = models / "news_alpha_predictor_candidate"
    active = models / "news_alpha_predictor"
    _write_model(candidate, auc=0.60, rows=500, val_rows=100)
    _write_model(active, auc=0.62, rows=500, val_rows=100)
    active_dir.mkdir(parents=True)
    (active_dir / "news_alpha_predictor.v1.json").write_text(
        json.dumps(
            {
                "agent_id": "news_alpha_predictor.v1",
                "model_name": "news_alpha_predictor",
                "promoted_at": time.time(),
                "promoted_by": "test",
            }
        )
    )

    report = evaluate_candidate(
        candidate_dir=candidate,
        models_dir=models,
        active_dir=active_dir,
        policy=CandidateGatePolicy(
            min_auc=0.55, min_rows=200, min_val_rows=40, min_auc_delta=0.0
        ),
    )

    assert report.approved is False
    assert report.active_model_name == "news_alpha_predictor"
    assert any("active threshold" in reason for reason in report.reasons)


def test_write_report_creates_json_file(tmp_path: pathlib.Path) -> None:
    models = tmp_path / "models"
    candidate = models / "news_alpha_predictor_candidate"
    _write_model(candidate)
    report = evaluate_candidate(candidate_dir=candidate, models_dir=models)
    report_path = tmp_path / "reports" / "candidate.json"

    write_report(report, report_path)

    payload = json.loads(report_path.read_text())
    assert payload["candidate_model_name"] == "news_alpha_predictor_candidate"
    assert "promotion_hint" in payload
