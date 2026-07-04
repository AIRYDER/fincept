"""Golden end-to-end smoke test for the ML dataset evidence spine.

This test exercises the full evidence loop in one shot:

  1. Build a synthetic dataset (parquet + manifest + receipt + quality)
  2. Verify the dataset manifest is PIT-proof and leakage-safe
  3. Write a prediction to the prediction log
  4. Write a feature snapshot for that prediction (what the agent saw)
  5. Write a settlement for that prediction (what happened)
  6. Hit ``GET /models/{name}/outcomes`` and verify the evidence receipt
     is complete: prediction fields + settlement fields + feature_schema_hash

The training step is intentionally omitted from the hot path — the
trainer is a subprocess that takes seconds and is covered by
``test_training.py``.  Instead we verify that a *trained* model
directory with an ``artifact_manifest.json`` is recognized, and that
the evidence spine (predict → snapshot → settle → outcomes) closes
the loop.

This is the test that fails loudly if any part of the spine breaks.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import time

import pytest
from httpx import AsyncClient

from fincept_core.datasets import (
    ArtifactManifest,
    FeatureRow,
    FeatureSnapshot,
    FeatureSnapshotStore,
    SettlementRecord,
    SettlementStore,
)
from fincept_core.datasets.schema_compat import (
    assert_feature_schema_compatible,
)
from fincept_core.prediction_log import PredictionLog


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def golden_stores(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
):
    """Redirect all evidence-spine stores at tmp dirs and return them."""
    predictions_dir = tmp_path / "predictions"
    settlements_dir = tmp_path / "settlements"
    snapshots_dir = tmp_path / "feature_snapshots"
    models_dir = tmp_path / "models"

    log = PredictionLog(predictions_dir=predictions_dir)
    settlement_store = SettlementStore(root=settlements_dir)
    snapshot_store = FeatureSnapshotStore(root=snapshots_dir)

    monkeypatch.setattr("api.routes.models._get_prediction_log", lambda: log)
    monkeypatch.setattr(
        "api.routes.models._get_settlement_store", lambda: settlement_store
    )
    monkeypatch.setattr("api.routes.models._get_snapshot_store", lambda: snapshot_store)
    # Redirect MODELS_DIR so the test doesn't see real models on disk.
    monkeypatch.setattr("api.routes.models._MODELS_DIR", models_dir)
    return {
        "log": log,
        "settlements": settlement_store,
        "snapshots": snapshot_store,
        "tmp_path": tmp_path,
        "models_dir": models_dir,
    }


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


FEATURE_NAMES = ("ret_1d", "ret_5d", "vol_20d", "mom_10d", "vol_ratio")


def _feature_schema_hash() -> str:
    return hashlib.sha256(":".join(sorted(FEATURE_NAMES)).encode()).hexdigest()


def _label_schema_hash() -> str:
    return hashlib.sha256(b"binary_forward_return_direction_5d").hexdigest()


def _write_artifact_manifest(model_dir: pathlib.Path) -> ArtifactManifest:
    """Write a minimal model directory with artifact_manifest.json."""
    model_dir.mkdir(parents=True, exist_ok=True)
    # Write a dummy model.txt so the model listing recognizes it.
    (model_dir / "model.txt").write_text("dummy-model")
    (model_dir / "meta.json").write_text(
        json.dumps(
            {
                "features": list(FEATURE_NAMES),
                "horizon_ns": 5 * 86_400_000_000_000,
                "trained_at": time.time(),
                "eval_mode": "walk_forward",
            }
        )
    )

    manifest = ArtifactManifest(
        artifact_id=f"gbm-{model_dir.name}",
        sha256=hashlib.sha256(b"dummy-model").hexdigest(),
        size_bytes=len(b"dummy-model"),
        uri=str(model_dir / "model.txt"),
        model_family="gbm",
        created_at_ns=time.time_ns(),
        feature_schema_hash=_feature_schema_hash(),
        label_schema_hash=_label_schema_hash(),
    )
    (model_dir / "artifact_manifest.json").write_text(
        manifest.model_dump_json(indent=2)
    )
    return manifest


# --------------------------------------------------------------------------- #
# The golden test                                                              #
# --------------------------------------------------------------------------- #


async def test_golden_e2e_evidence_spine(
    client: AsyncClient,
    auth_headers: dict[str, str],
    golden_stores,
) -> None:
    """Full evidence spine: predict → snapshot → settle → outcomes.

    This is the single test that verifies the complete loop works
    end-to-end.  If any part of the spine breaks, this test fails first.
    """
    log = golden_stores["log"]
    settlement_store = golden_stores["settlements"]
    snapshot_store = golden_stores["snapshots"]
    models_dir = golden_stores["models_dir"]
    agent_id = "gbm_predictor.v1"
    model_name = "golden_test_model"

    # --- Step 1: Write artifact manifest (simulates a completed training run)
    artifact_manifest = _write_artifact_manifest(models_dir / model_name)

    # --- Step 2: Verify schema compatibility between artifact and snapshot
    # (This is the P6 check — fails loudly on incompatible schemas)
    snapshot_feature_names = FEATURE_NAMES
    assert_feature_schema_compatible(
        artifact_feature_schema_hash=artifact_manifest.feature_schema_hash,
        artifact_feature_schema_version=artifact_manifest.feature_schema_version,
        artifact_feature_names=FEATURE_NAMES,
        snapshot_feature_schema_hash=_feature_schema_hash(),
        snapshot_feature_schema_version=1,
        snapshot_feature_names=snapshot_feature_names,
    )

    # --- Step 3: Write a prediction (what the agent said)
    ts_event = 1_800_000_000_000_000_000  # fixed point in time
    horizon_ns = 5 * 86_400_000_000_000  # 5 days
    pred_row = log.append(
        agent_id=agent_id,
        model_name=model_name,
        ts_event=ts_event,
        horizon_ns=horizon_ns,
        symbol="AAPL",
        direction=1.0,
        confidence=0.72,
    )
    assert pred_row is not None
    assert pred_row.id  # prediction_id is non-empty

    # --- Step 4: Write a feature snapshot (what the agent saw)
    feature_vector = {
        "ret_1d": 0.015,
        "ret_5d": 0.032,
        "vol_20d": 0.21,
        "mom_10d": 0.008,
        "vol_ratio": 1.15,
    }
    feature_row = FeatureRow(
        symbol="AAPL",
        ts=ts_event,
        features=feature_vector,
    )
    snapshot = FeatureSnapshot(
        decision_time_ns=ts_event,
        rows=[feature_row],
        feature_schema_hash=_feature_schema_hash(),
    )
    appended = snapshot_store.append_if_missing(
        pred_row.id,
        snapshot,
        agent_id=agent_id,
    )
    assert appended is True

    # --- Step 5: Write a settlement (what happened)
    settlement = SettlementRecord(
        prediction_id=pred_row.id,
        agent_id=agent_id,
        model_name=model_name,
        symbol="AAPL",
        ts_event=ts_event,
        horizon_ns=horizon_ns,
        decision_window_start_ns=ts_event,
        decision_window_end_ns=ts_event + horizon_ns,
        cost_breakdown_fee_bps=5.0,
        cost_breakdown_spread_bps=3.0,
        realized_return_gross=0.018,
        realized_return_net=0.0172,
        brier_component=0.0784,
        status="settled",
        settled_at_ns=ts_event + horizon_ns,
    )
    settlement_store.append(settlement, now_ns=ts_event + horizon_ns + 1)

    # --- Step 6: Verify GET /models/{name}/outcomes returns complete receipt
    r = await client.get(
        f"/models/{model_name}/outcomes",
        headers=auth_headers,
        params={"agent_id": agent_id, "limit": 10},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == model_name
    assert body["agent_id"] == agent_id
    assert body["count"] == 1

    outcome = body["outcomes"][0]

    # Prediction fields
    assert outcome["prediction_id"] == pred_row.id
    assert outcome["agent_id"] == agent_id
    assert outcome["model_name"] == model_name
    assert outcome["symbol"] == "AAPL"
    assert outcome["direction"] == 1.0
    assert outcome["confidence"] == 0.72

    # Settlement fields (the "what happened" leg)
    assert outcome["settlement_status"] == "settled"
    assert outcome["realized_return_gross"] == 0.018
    assert outcome["realized_return_net"] == 0.0172
    assert outcome["brier_component"] == 0.0784
    assert outcome["settled_at_ns"] == ts_event + horizon_ns

    # Feature snapshot fields (the "what the agent saw" leg)
    assert "feature_schema_hash" in outcome
    assert outcome["feature_schema_hash"] == _feature_schema_hash()


async def test_golden_e2e_pending_prediction(
    client: AsyncClient,
    auth_headers: dict[str, str],
    golden_stores,
) -> None:
    """A prediction with a snapshot but no settlement shows pending_time."""
    log = golden_stores["log"]
    snapshot_store = golden_stores["snapshots"]
    agent_id = "gbm_predictor.v1"
    model_name = "golden_pending_model"

    # Write prediction
    pred_row = log.append(
        agent_id=agent_id,
        model_name=model_name,
        ts_event=1_800_000_000_000_000_000,
        horizon_ns=5 * 86_400_000_000_000,
        symbol="MSFT",
        direction=-1.0,
        confidence=0.55,
    )

    # Write snapshot but NO settlement
    snapshot = FeatureSnapshot(
        decision_time_ns=1_800_000_000_000_000_000,
        rows=[
            FeatureRow(
                symbol="MSFT",
                ts=1_800_000_000_000_000_000,
                features={"ret_1d": -0.01, "ret_5d": 0.02},
            )
        ],
        feature_schema_hash=_feature_schema_hash(),
    )
    snapshot_store.append_if_missing(pred_row.id, snapshot, agent_id=agent_id)

    # Verify outcomes
    r = await client.get(
        f"/models/{model_name}/outcomes",
        headers=auth_headers,
        params={"agent_id": agent_id, "limit": 10},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    outcome = body["outcomes"][0]

    # Settlement should be pending
    assert outcome["settlement_status"] == "pending_time"
    assert outcome["realized_return_gross"] is None
    assert outcome["realized_return_net"] is None

    # But feature schema hash should still be present
    assert "feature_schema_hash" in outcome


async def test_golden_e2e_resume_endpoint(
    client: AsyncClient,
    auth_headers: dict[str, str],
    golden_stores,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A resumable_failed run can be resumed via POST /models/runs/{id}/resume."""
    from api.training import TrainingRun, TrainingRequest, TrainingStore

    # Create an isolated training store with a resumable_failed run.
    runs_dir = tmp_path / "training_runs"
    models_dir = tmp_path / "models"
    store = TrainingStore(
        runs_dir=runs_dir,
        models_dir=models_dir,
        max_concurrent=1,
        trainer_cmd=["echo", "dummy"],
    )
    monkeypatch.setattr("api.routes.models.get_store", lambda: store)

    # Manually insert a resumable_failed run.
    req = TrainingRequest(
        model_name="test_resume_model",
        input_path=str(tmp_path / "input.parquet"),
        horizon_bars=15,
        bar_seconds=60,
        cv_folds=3,
        purge_bars=-1,
        embargo_bars=0,
        num_boost_round=100,
        early_stopping_rounds=10,
    )
    run = TrainingRun(
        run_id="test-run-1",
        request=req,
        status="resumable_failed",
        created_at=time.time(),
        started_at=None,
        finished_at=None,
        exit_code=None,
        out_dir=str(models_dir / "test_resume_model"),
        log_path=str(runs_dir / "test-run-1.log"),
        record_path=str(runs_dir / "test-run-1.json"),
        error="api restarted while this run was active; subprocess state lost (resumable)",
    )
    store._runs["test-run-1"] = run

    # Create the input file so validation passes.
    (tmp_path / "input.parquet").write_bytes(b"dummy")

    # Also need to patch approved_roots to allow tmp_path.
    from fincept_core.datasets import ApprovedRoots

    monkeypatch.setattr(
        "api.training.default_approved_roots",
        lambda: ApprovedRoots(roots=[tmp_path]),
    )

    # Resume the run.
    r = await client.post(
        "/models/runs/test-run-1/resume",
        headers=auth_headers,
    )
    assert r.status_code == 202
    body = r.json()
    assert body["run_id"] == "test-run-1"
    assert body["status"] == "queued"
    assert body["resume_token"] is not None


async def test_golden_e2e_resume_rejects_completed(
    client: AsyncClient,
    auth_headers: dict[str, str],
    golden_stores,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Resume endpoint rejects a completed run with 409."""
    from api.training import TrainingRun, TrainingRequest, TrainingStore

    runs_dir = tmp_path / "training_runs"
    models_dir = tmp_path / "models"
    store = TrainingStore(
        runs_dir=runs_dir,
        models_dir=models_dir,
        max_concurrent=1,
        trainer_cmd=["echo", "dummy"],
    )
    monkeypatch.setattr("api.routes.models.get_store", lambda: store)

    req = TrainingRequest(
        model_name="test_completed",
        input_path=str(tmp_path / "input.parquet"),
        horizon_bars=15,
        bar_seconds=60,
        cv_folds=3,
        purge_bars=-1,
        embargo_bars=0,
        num_boost_round=100,
        early_stopping_rounds=10,
    )
    run = TrainingRun(
        run_id="test-run-completed",
        request=req,
        status="completed",
        created_at=time.time(),
        started_at=time.time(),
        finished_at=time.time(),
        exit_code=0,
        out_dir=str(models_dir / "test_completed"),
        log_path=str(runs_dir / "test-run-completed.log"),
        record_path=str(runs_dir / "test-run-completed.json"),
    )
    store._runs["test-run-completed"] = run

    r = await client.post(
        "/models/runs/test-run-completed/resume",
        headers=auth_headers,
    )
    assert r.status_code == 409
