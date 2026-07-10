"""Tests for the ``fincept_core.datasets`` package facade (todo 5).

These tests pin the stable public surface of the evidence-spine facade:
the re-exports resolve, a missing name raises ``ImportError``, the
``build_evidence_receipt`` helper joins prediction + settlement fields
correctly, and the not-yet-implemented CV names (``make_folds`` etc.)
are at least bound in the module namespace so callers get a stable
facade from day one.
"""

from __future__ import annotations

import pytest

import fincept_core.datasets as datasets_mod
from fincept_core.datasets import (
    ApprovedRoots,
    DatasetManifest,
    FeatureSnapshot,
    FeatureSnapshotStore,
    SettlementRecord,
    SettlementStore,
    build_evidence_receipt,
)
from fincept_core.prediction_log import PredictionRow

# --------------------------------------------------------------------------- #
# Facade re-exports                                                           #
# --------------------------------------------------------------------------- #


def test_facade_imports_succeed() -> None:
    """The headline public names import cleanly from the package root."""
    # If this line executes at all the import succeeded; assert they are
    # the real objects (not None) to catch an accidental stub.
    assert ApprovedRoots is not None
    assert SettlementStore is not None
    assert DatasetManifest is not None
    assert FeatureSnapshot is not None
    assert FeatureSnapshotStore is not None


def test_facade_missing_import_raises() -> None:
    """A name that is not part of the public surface raises ImportError."""
    with pytest.raises(ImportError):
        pass


def test_make_folds_name_present() -> None:
    """``make_folds`` is bound in the module namespace (todo 17 fills it in).

    Until todo 17 lands ``cv.py`` the name is ``None``, but ``hasattr``
    must still be ``True`` so the facade is stable from day one.
    """
    assert hasattr(datasets_mod, "make_folds")
    assert hasattr(datasets_mod, "Fold")
    assert hasattr(datasets_mod, "fold_iter_to_dicts")


# --------------------------------------------------------------------------- #
# build_evidence_receipt                                                       #
# --------------------------------------------------------------------------- #


def _make_prediction() -> PredictionRow:
    return PredictionRow(
        id="pred-123",
        agent_id="agent-a",
        model_name="model-x",
        ts_recorded=1_000_000,
        ts_event=1_000_000,
        horizon_ns=60_000_000_000,
        symbol="AAPL",
        direction=1.0,
        confidence=0.7,
    )


def _make_settlement() -> SettlementRecord:
    return SettlementRecord(
        prediction_id="pred-123",
        agent_id="agent-a",
        model_name="model-x",
        symbol="AAPL",
        ts_event=1_000_000,
        horizon_ns=60_000_000_000,
        decision_window_start_ns=1_000_000,
        decision_window_end_ns=61_000_000,
        cost_breakdown_fee_bps=5.0,
        cost_breakdown_spread_bps=3.0,
        realized_return_gross=0.0012,
        realized_return_net=0.0004,
        brier_component=0.09,
        status="settled",
        settled_at_ns=61_000_001,
    )


def test_build_evidence_receipt_with_settlement() -> None:
    """A settled prediction joins prediction + settlement fields flatly."""
    pred = _make_prediction()
    sett = _make_settlement()
    receipt = build_evidence_receipt(
        prediction=pred,
        settlement=sett,
        feature_snapshot=None,
    )
    assert receipt["prediction_id"] == "pred-123"
    assert receipt["agent_id"] == "agent-a"
    assert receipt["model_name"] == "model-x"
    assert receipt["ts_event"] == 1_000_000
    assert receipt["horizon_ns"] == 60_000_000_000
    assert receipt["symbol"] == "AAPL"
    assert receipt["direction"] == 1.0
    assert receipt["confidence"] == 0.7
    assert receipt["settlement_status"] == "settled"
    assert receipt["realized_return_gross"] == 0.0012
    assert receipt["realized_return_net"] == 0.0004
    assert receipt["settled_at_ns"] == 61_000_001
    assert receipt["brier_component"] == 0.09
    # No snapshot / health supplied -> those keys absent.
    assert "feature_schema_hash" not in receipt
    assert "feature_health" not in receipt


def test_build_evidence_receipt_pending() -> None:
    """A prediction with no settlement reports ``pending_time``."""
    pred = _make_prediction()
    receipt = build_evidence_receipt(
        prediction=pred,
        settlement=None,
        feature_snapshot=None,
    )
    assert receipt["settlement_status"] == "pending_time"
    assert receipt["realized_return_gross"] is None
    assert receipt["realized_return_net"] is None
    assert receipt["settled_at_ns"] is None
    assert receipt["brier_component"] is None
    # Prediction fields still present.
    assert receipt["prediction_id"] == "pred-123"
    assert receipt["symbol"] == "AAPL"


def test_build_evidence_receipt_with_snapshot_and_health() -> None:
    """A feature snapshot contributes its schema hash; health is passed through."""
    pred = _make_prediction()
    snapshot = FeatureSnapshot(
        decision_time_ns=999_999,
        rows=[],
        feature_schema_hash="a" * 64,
    )
    health = {"stale": 3, "missing": 1}
    receipt = build_evidence_receipt(
        prediction=pred,
        settlement=None,
        feature_snapshot=snapshot,
        feature_health=health,
    )
    assert receipt["feature_schema_hash"] == "a" * 64
    assert receipt["feature_health"] == health
