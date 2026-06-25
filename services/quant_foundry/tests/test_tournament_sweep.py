"""
TDD tests for the tournament sweep worker (TASK-B1).

Covers:
- Fixture settlement records → deterministic tournament scores.
- Insufficient evidence → INSUFFICIENT_EVIDENCE blocked model.
- Stale records → STALE flag.
- Sweep populates the ExpandedLeaderboard.
- Receipt carries scored/blocked/stale lists + trial_count.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any

import pytest

from quant_foundry.dossier import DossierRecord, DossierStatus
from quant_foundry.leaderboard_expanded import ExpandedLeaderboard
from quant_foundry.outcomes import SettlementRecord, SettlementStatus
from quant_foundry.registry import DossierRegistry
from quant_foundry.tournament import Tournament, TournamentStatus
from quant_foundry.tournament_sweep import TournamentSweep


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSettlementLedger:
    """In-memory settlement ledger for tests."""

    def __init__(self, records: list[SettlementRecord] | None = None) -> None:
        self._records = records or []

    def read_all(self) -> list[SettlementRecord]:
        return list(self._records)


def _settled_record(
    *,
    prediction_id: str,
    model_id: str,
    realized_net: float,
    realized_gross: float | None = None,
    brier: float | None = None,
    calibration_bucket: str | None = None,
    settled_at_ns: int = 1_000_000_000,
    cost_model_version: str = "cm-v1",
    symbol: str = "BTC-USD",
) -> SettlementRecord:
    return SettlementRecord(
        prediction_id=prediction_id,
        model_id=model_id,
        symbol=symbol,
        ts_event=settled_at_ns - 1000,
        horizon_ns=86_400_000_000_000,
        status=SettlementStatus.SETTLED,
        settled_at_ns=settled_at_ns,
        realized_return_gross=realized_gross if realized_gross is not None else realized_net,
        realized_return_net=realized_net,
        abnormal_return=None,
        brier=brier,
        calibration_bucket=calibration_bucket,
        cost_model_version=cost_model_version,
        decision_window_start=settled_at_ns - 1000,
        decision_window_end=settled_at_ns,
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tournament() -> Tournament:
    return Tournament(seed=42, n_bootstrap=100)


@pytest.fixture
def leaderboard() -> ExpandedLeaderboard:
    return ExpandedLeaderboard()


@pytest.fixture
def dossier_registry(tmp_path: pathlib.Path) -> DossierRegistry:
    return DossierRegistry(tmp_path / "dossier_registry")


def _make_records(
    model_id: str,
    count: int,
    *,
    net: float = 0.001,
    settled_at_ns: int = 1_000_000_000,
) -> list[SettlementRecord]:
    return [
        _settled_record(
            prediction_id=f"{model_id}-pred-{i}",
            model_id=model_id,
            realized_net=net,
            brier=0.2,
            calibration_bucket="medium",
            settled_at_ns=settled_at_ns + i,
        )
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# Tests: sufficient evidence → scored
# ---------------------------------------------------------------------------


def test_sweep_scores_model_with_sufficient_evidence(
    tournament: Tournament,
    leaderboard: ExpandedLeaderboard,
    dossier_registry: DossierRegistry,
) -> None:
    # Given: a model with 12 settled records (>= min_settled_samples=10).
    dossier_registry.register(_dossier("model-a", trial_count=3))
    records = _make_records("model-a", 12, net=0.002)
    ledger = FakeSettlementLedger(records)

    sweep = TournamentSweep(
        settlement_ledger=ledger,
        dossier_registry=dossier_registry,
        tournament=tournament,
        leaderboard=leaderboard,
        min_settled_samples=10,
    )

    # When: a sweep is run.
    receipt = sweep.sweep(now_ns=2_000_000_000)

    # Then: the model is scored and added to the leaderboard.
    assert len(receipt.scored_models) == 1
    assert receipt.scored_models[0].model_id == "model-a"
    assert receipt.blocked_models == []
    assert receipt.stale_models == []
    assert receipt.trial_count == 1

    ranked = leaderboard.ranked()
    assert len(ranked) == 1
    assert ranked[0].model_id == "model-a"
    assert ranked[0].settled_count == 12


def test_sweep_deterministic_scores(
    tournament: Tournament,
    dossier_registry: DossierRegistry,
) -> None:
    # Given: the same settlement records.
    dossier_registry.register(_dossier("model-d"))
    records = _make_records("model-d", 15, net=0.003)

    # When: two sweeps are run with the same now_ns.
    lb1 = ExpandedLeaderboard()
    sweep1 = TournamentSweep(
        settlement_ledger=FakeSettlementLedger(records),
        dossier_registry=dossier_registry,
        tournament=tournament,
        leaderboard=lb1,
    )
    r1 = sweep1.sweep(now_ns=2_000_000_000)

    lb2 = ExpandedLeaderboard()
    sweep2 = TournamentSweep(
        settlement_ledger=FakeSettlementLedger(records),
        dossier_registry=dossier_registry,
        tournament=tournament,
        leaderboard=lb2,
    )
    r2 = sweep2.sweep(now_ns=2_000_000_000)

    # Then: the scores are identical (deterministic).
    assert r1.scored_models[0].tournament_result["total_score"] == pytest.approx(
        r2.scored_models[0].tournament_result["total_score"]
    )


# ---------------------------------------------------------------------------
# Tests: insufficient evidence → blocked
# ---------------------------------------------------------------------------


def test_sweep_blocks_model_with_insufficient_evidence(
    tournament: Tournament,
    leaderboard: ExpandedLeaderboard,
    dossier_registry: DossierRegistry,
) -> None:
    # Given: a model with only 5 settled records (< min_settled_samples=10).
    dossier_registry.register(_dossier("model-few"))
    records = _make_records("model-few", 5)
    ledger = FakeSettlementLedger(records)

    sweep = TournamentSweep(
        settlement_ledger=ledger,
        dossier_registry=dossier_registry,
        tournament=tournament,
        leaderboard=leaderboard,
        min_settled_samples=10,
    )

    # When: a sweep is run.
    receipt = sweep.sweep(now_ns=2_000_000_000)

    # Then: the model is blocked with INSUFFICIENT_EVIDENCE.
    assert receipt.scored_models == []
    assert len(receipt.blocked_models) == 1
    blocked = receipt.blocked_models[0]
    assert blocked.model_id == "model-few"
    assert blocked.status == TournamentStatus.INSUFFICIENT_EVIDENCE.value
    assert blocked.settled_count == 5
    assert "min_settled_samples" in blocked.reason

    # And: the leaderboard is not populated for blocked models.
    assert leaderboard.ranked() == []


# ---------------------------------------------------------------------------
# Tests: stale records → STALE
# ---------------------------------------------------------------------------


def test_sweep_flags_stale_model(
    tournament: Tournament,
    leaderboard: ExpandedLeaderboard,
    dossier_registry: DossierRegistry,
) -> None:
    # Given: a model with 12 settled records, but the last settlement is old.
    dossier_registry.register(_dossier("model-stale"))
    stale_threshold_ns = 7 * 24 * 3600 * 1_000_000_000  # 7 days
    old_settled_ns = 1_000_000_000
    records = _make_records("model-stale", 12, settled_at_ns=old_settled_ns)
    ledger = FakeSettlementLedger(records)

    # now_ns is 30 days later → stale.
    now_ns = old_settled_ns + 30 * 24 * 3600 * 1_000_000_000

    sweep = TournamentSweep(
        settlement_ledger=ledger,
        dossier_registry=dossier_registry,
        tournament=tournament,
        leaderboard=leaderboard,
        min_settled_samples=10,
        stale_threshold_ns=stale_threshold_ns,
    )

    # When: a sweep is run.
    receipt = sweep.sweep(now_ns=now_ns)

    # Then: the model is scored but also flagged as stale.
    assert len(receipt.scored_models) == 1
    assert len(receipt.stale_models) == 1
    stale = receipt.stale_models[0]
    assert stale.model_id == "model-stale"
    assert stale.age_ns > stale_threshold_ns

    # And: the leaderboard entry is flagged as stale.
    ranked = leaderboard.ranked()
    assert len(ranked) == 1
    assert ranked[0].decay_indicator is not None
    assert ranked[0].decay_indicator.is_stale is True


# ---------------------------------------------------------------------------
# Tests: multiple models + leaderboard ranking
# ---------------------------------------------------------------------------


def test_sweep_scores_multiple_models_and_ranks(
    tournament: Tournament,
    leaderboard: ExpandedLeaderboard,
    dossier_registry: DossierRegistry,
) -> None:
    # Given: two models with sufficient evidence, one with higher net returns.
    dossier_registry.register(_dossier("model-strong", trial_count=2))
    dossier_registry.register(_dossier("model-weak", trial_count=2))
    strong_records = _make_records("model-strong", 15, net=0.005)
    weak_records = _make_records("model-weak", 15, net=0.001)
    ledger = FakeSettlementLedger(strong_records + weak_records)

    sweep = TournamentSweep(
        settlement_ledger=ledger,
        dossier_registry=dossier_registry,
        tournament=tournament,
        leaderboard=leaderboard,
        min_settled_samples=10,
    )

    # When: a sweep is run.
    receipt = sweep.sweep(now_ns=2_000_000_000)

    # Then: both models are scored.
    assert len(receipt.scored_models) == 2
    assert receipt.trial_count == 2

    # And: the leaderboard ranks the stronger model first.
    ranked = leaderboard.ranked()
    assert len(ranked) == 2
    assert ranked[0].total_score >= ranked[1].total_score


# ---------------------------------------------------------------------------
# Tests: receipt shape
# ---------------------------------------------------------------------------


def test_sweep_receipt_to_dict_is_json_serializable(
    tournament: Tournament,
    leaderboard: ExpandedLeaderboard,
    dossier_registry: DossierRegistry,
) -> None:
    import json

    dossier_registry.register(_dossier("model-json"))
    records = _make_records("model-json", 12)
    ledger = FakeSettlementLedger(records)

    sweep = TournamentSweep(
        settlement_ledger=ledger,
        dossier_registry=dossier_registry,
        tournament=tournament,
        leaderboard=leaderboard,
    )
    receipt = sweep.sweep(now_ns=2_000_000_000)

    # Then: the receipt dict is JSON serializable (no secrets, no raw objects).
    d = receipt.to_dict()
    json.dumps(d)
    assert "scored_models" in d
    assert "blocked_models" in d
    assert "stale_models" in d
    assert "trial_count" in d
    assert "swept_at_ns" in d


# ---------------------------------------------------------------------------
# Tests: empty ledger
# ---------------------------------------------------------------------------


def test_sweep_empty_ledger_returns_empty_receipt(
    tournament: Tournament,
    leaderboard: ExpandedLeaderboard,
    dossier_registry: DossierRegistry,
) -> None:
    ledger = FakeSettlementLedger([])
    sweep = TournamentSweep(
        settlement_ledger=ledger,
        dossier_registry=dossier_registry,
        tournament=tournament,
        leaderboard=leaderboard,
    )

    receipt = sweep.sweep(now_ns=2_000_000_000)

    assert receipt.scored_models == []
    assert receipt.blocked_models == []
    assert receipt.stale_models == []
    assert receipt.trial_count == 0
