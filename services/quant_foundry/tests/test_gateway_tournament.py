"""
TDD tests for gateway tournament wiring (TASK-B2).

Covers:
- run_tournament_sweep() returns a receipt with scored/blocked/stale lists.
- tournament_leaderboard() returns real ranked entries after a sweep.
- tournament_status() returns a summary with counts + leaderboard.
- Disabled gateway returns safe empty states.
- tournament_sweep() lazy-init constructs the worker.
"""

from __future__ import annotations

import pathlib
import time

import pytest

from quant_foundry.dossier import DossierRecord, DossierStatus
from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.outcomes import SettlementRecord, SettlementStatus


def _gateway(base_dir: pathlib.Path, *, enabled: bool = True) -> QuantFoundryGateway:
    return QuantFoundryGateway(
        enabled=enabled,
        mode="local_mock",
        shadow_only=True,
        callback_secret="test-cb-secret",
        base_dir=base_dir,
    )


def _dossier(model_id: str, *, trial_count: int = 3) -> DossierRecord:
    return DossierRecord(
        model_id=model_id,
        artifact_manifest_id=f"artifact-{model_id}",
        artifact_sha256=f"sha256-{model_id}",
        dataset_manifest_id="dataset-test",
        feature_schema_hash="fs-hash",
        label_schema_hash="ls-hash",
        trial_count=trial_count,
        status=DossierStatus.CANDIDATE,
    )


def _settled_record(
    *,
    prediction_id: str,
    model_id: str,
    realized_net: float = 0.002,
    settled_at_ns: int | None = None,
) -> SettlementRecord:
    ts = settled_at_ns if settled_at_ns is not None else time.time_ns()
    return SettlementRecord(
        prediction_id=prediction_id,
        model_id=model_id,
        symbol="BTC-USD",
        ts_event=ts - 1000,
        horizon_ns=86_400_000_000_000,
        status=SettlementStatus.SETTLED,
        settled_at_ns=ts,
        realized_return_gross=realized_net,
        realized_return_net=realized_net,
        abnormal_return=None,
        brier=0.2,
        calibration_bucket="medium",
        cost_model_version="cm-v1",
        decision_window_start=ts - 1000,
        decision_window_end=ts,
    )


def _write_settlements(base_dir: pathlib.Path, model_id: str, count: int) -> None:
    """Write settlement records directly to the ledger file."""
    ledger_dir = base_dir / "settlements"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_dir / f"{model_id}.settlements.jsonl"
    now_ns = time.time_ns()
    with path.open("a", encoding="utf-8") as f:
        for i in range(count):
            rec = _settled_record(
                prediction_id=f"{model_id}-pred-{i}",
                model_id=model_id,
                settled_at_ns=now_ns - i * 1_000_000_000,
            )
            f.write(rec.to_json() + "\n")


# ---------------------------------------------------------------------------
# Gateway unit tests
# ---------------------------------------------------------------------------


def test_run_tournament_sweep_scores_model(tmp_path: pathlib.Path) -> None:
    gw = _gateway(tmp_path / "qf")
    gw.dossier_registry().register(_dossier("model-a"))
    _write_settlements(tmp_path / "qf", "model-a", 12)

    result = gw.run_tournament_sweep()

    assert result["enabled"] is True
    assert len(result["scored_models"]) == 1
    assert result["scored_models"][0]["model_id"] == "model-a"
    assert result["blocked_models"] == []
    assert result["stale_models"] == []
    assert result["trial_count"] == 1


def test_run_tournament_sweep_blocks_insufficient_evidence(
    tmp_path: pathlib.Path,
) -> None:
    gw = _gateway(tmp_path / "qf")
    gw.dossier_registry().register(_dossier("model-few"))
    _write_settlements(tmp_path / "qf", "model-few", 5)

    result = gw.run_tournament_sweep()

    assert result["scored_models"] == []
    assert len(result["blocked_models"]) == 1
    assert result["blocked_models"][0]["model_id"] == "model-few"
    assert result["blocked_models"][0]["status"] == "insufficient_evidence"


def test_tournament_leaderboard_returns_ranked_entries(
    tmp_path: pathlib.Path,
) -> None:
    gw = _gateway(tmp_path / "qf")
    gw.dossier_registry().register(_dossier("model-lb"))
    _write_settlements(tmp_path / "qf", "model-lb", 12)

    # Before sweep: empty leaderboard.
    assert gw.tournament_leaderboard() == []

    gw.run_tournament_sweep()

    # After sweep: leaderboard has the ranked entry.
    lb = gw.tournament_leaderboard()
    assert len(lb) == 1
    assert lb[0]["model_id"] == "model-lb"
    assert lb[0]["settled_count"] == 12


def test_tournament_status_returns_summary(tmp_path: pathlib.Path) -> None:
    gw = _gateway(tmp_path / "qf")
    gw.dossier_registry().register(_dossier("model-ts"))
    _write_settlements(tmp_path / "qf", "model-ts", 12)

    gw.run_tournament_sweep()

    status = gw.tournament_status()
    assert status["enabled"] is True
    assert status["scored"] == 1
    assert status["stale"] == 0
    assert len(status["leaderboard"]) == 1


def test_disabled_gateway_run_sweep_returns_disabled(tmp_path: pathlib.Path) -> None:
    gw = _gateway(tmp_path / "qf", enabled=False)
    result = gw.run_tournament_sweep()
    assert result["enabled"] is False


def test_disabled_gateway_tournament_status_returns_disabled(
    tmp_path: pathlib.Path,
) -> None:
    gw = _gateway(tmp_path / "qf", enabled=False)
    status = gw.tournament_status()
    assert status["enabled"] is False
    assert status["scored"] == 0


def test_tournament_sweep_lazy_init(tmp_path: pathlib.Path) -> None:
    gw = _gateway(tmp_path / "qf")
    sweep1 = gw.tournament_sweep()
    sweep2 = gw.tournament_sweep()
    assert sweep1 is sweep2


def test_run_tournament_sweep_multiple_models(tmp_path: pathlib.Path) -> None:
    gw = _gateway(tmp_path / "qf")
    gw.dossier_registry().register(_dossier("model-strong"))
    gw.dossier_registry().register(_dossier("model-weak"))
    _write_settlements(tmp_path / "qf", "model-strong", 15)
    _write_settlements(tmp_path / "qf", "model-weak", 12)

    result = gw.run_tournament_sweep()

    assert result["enabled"] is True
    assert len(result["scored_models"]) == 2
    assert result["trial_count"] == 2

    # Leaderboard should have both entries.
    lb = gw.tournament_leaderboard()
    assert len(lb) == 2
    model_ids = {e["model_id"] for e in lb}
    assert model_ids == {"model-strong", "model-weak"}
