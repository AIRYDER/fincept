"""Tests for ``GET /models/{name}/outcomes`` (todo 8).

The outcomes route left-joins the prediction log with the settlement
side-store by ``prediction_id``.  These tests cover:

  * Empty store (no predictions, no settlements) → ``[]``.
  * Predictions with matching settlements → ``settlement_status: "settled"``.
  * Predictions without settlements → ``settlement_status: "pending_time"``.
  * Pagination via ``since_ns``.
  * Missing data files (no JSONL) → ``[]`` with no crash.
  * Malformed settlement JSONL line skipped with no 500.

The fixtures monkey-patch ``_get_prediction_log`` and
``_get_settlement_store`` to redirect at tmp directories, matching the
pattern in ``test_predictions.py``.
"""

from __future__ import annotations

import pathlib

import pytest
from httpx import AsyncClient


@pytest.fixture
def patched_stores(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
):
    """Redirect prediction log, settlement store, and snapshot store at tmp dirs.

    Yields a dict with the three store instances so each test can
    pre-populate them with the rows it cares about.
    """
    from fincept_core.datasets import (
        FeatureSnapshotStore,
        SettlementStore,
    )
    from fincept_core.prediction_log import PredictionLog

    predictions_dir = tmp_path / "predictions"
    settlements_dir = tmp_path / "settlements"
    snapshots_dir = tmp_path / "feature_snapshots"
    log = PredictionLog(predictions_dir=predictions_dir)
    settlement_store = SettlementStore(root=settlements_dir)
    snapshot_store = FeatureSnapshotStore(root=snapshots_dir)

    monkeypatch.setattr("api.routes.models._get_prediction_log", lambda: log)
    monkeypatch.setattr(
        "api.routes.models._get_settlement_store", lambda: settlement_store
    )
    monkeypatch.setattr("api.routes.models._get_snapshot_store", lambda: snapshot_store)
    return {
        "log": log,
        "settlements": settlement_store,
        "snapshots": snapshot_store,
        "tmp_path": tmp_path,
    }


def _seed_predictions(
    log,
    *,
    n: int,
    agent_id: str = "gbm_predictor.v1",
    model_name: str = "gbm_predictor",
    base_ts: int = 1_000_000_000,
) -> list:
    """Append ``n`` prediction rows and return them (for joining)."""
    rows = []
    for i in range(n):
        row = log.append(
            agent_id=agent_id,
            model_name=model_name,
            ts_event=base_ts + i,
            horizon_ns=15 * 60 * 1_000_000_000,
            symbol="BTC-USD",
            direction=0.5,
            confidence=0.6,
        )
        rows.append(row)
    return rows


def _make_settlement(
    prediction_row,
    *,
    agent_id: str = "gbm_predictor.v1",
    model_name: str = "gbm_predictor",
    status: str = "settled",
    now_ns: int = 2_000_000_000_000_000_000,
):
    """Build a SettlementRecord matching the given prediction row."""
    from fincept_core.datasets import SettlementRecord

    return SettlementRecord(
        prediction_id=prediction_row.id,
        agent_id=agent_id,
        model_name=model_name,
        symbol=prediction_row.symbol,
        ts_event=prediction_row.ts_event,
        horizon_ns=prediction_row.horizon_ns,
        decision_window_start_ns=prediction_row.ts_event,
        decision_window_end_ns=prediction_row.ts_event + prediction_row.horizon_ns,
        cost_breakdown_fee_bps=5.0,
        cost_breakdown_spread_bps=3.0,
        realized_return_gross=0.01 if status == "settled" else None,
        realized_return_net=0.0002 if status == "settled" else None,
        brier_component=0.16 if status == "settled" else None,
        status=status,
        settled_at_ns=now_ns if status == "settled" else None,
    )


# --------------------------------------------------------------------------- #
# Auth                                                                        #
# --------------------------------------------------------------------------- #


async def test_outcomes_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/models/gbm_predictor/outcomes")
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Empty store                                                                 #
# --------------------------------------------------------------------------- #


async def test_outcomes_empty_when_no_data(
    client: AsyncClient,
    auth_headers: dict[str, str],
    patched_stores,
) -> None:
    r = await client.get("/models/gbm_predictor/outcomes", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "gbm_predictor"
    assert body["agent_id"] == "gbm_predictor.v1"
    assert body["count"] == 0
    assert body["outcomes"] == []


# --------------------------------------------------------------------------- #
# Predictions with settlements                                                #
# --------------------------------------------------------------------------- #


async def test_outcomes_with_settlements(
    client: AsyncClient,
    auth_headers: dict[str, str],
    patched_stores,
) -> None:
    """3 predictions + 2 settlements → 3 rows, 2 settled + 1 pending_time."""
    log = patched_stores["log"]
    store = patched_stores["settlements"]

    preds = _seed_predictions(log, n=3)
    # Settle the first two; leave the third pending.
    store.append(_make_settlement(preds[0]))
    store.append(_make_settlement(preds[1]))

    r = await client.get("/models/gbm_predictor/outcomes", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3

    statuses = {o["prediction_id"]: o["settlement_status"] for o in body["outcomes"]}
    assert statuses[preds[0].id] == "settled"
    assert statuses[preds[1].id] == "settled"
    assert statuses[preds[2].id] == "pending_time"

    # Verify the settled row carries the expected fields.
    settled = next(o for o in body["outcomes"] if o["prediction_id"] == preds[0].id)
    assert settled["realized_return_gross"] == pytest.approx(0.01)
    assert settled["realized_return_net"] == pytest.approx(0.0002)
    assert settled["brier_component"] is not None
    assert settled["settled_at_ns"] is not None

    # Verify the pending row has null settlement fields.
    pending = next(o for o in body["outcomes"] if o["prediction_id"] == preds[2].id)
    assert pending["realized_return_gross"] is None
    assert pending["realized_return_net"] is None
    assert pending["settled_at_ns"] is None
    assert pending["brier_component"] is None


# --------------------------------------------------------------------------- #
# Predictions pending (no settlement file at all)                             #
# --------------------------------------------------------------------------- #


async def test_outcomes_all_pending_when_no_settlements(
    client: AsyncClient,
    auth_headers: dict[str, str],
    patched_stores,
) -> None:
    """Predictions exist but the settlement file is missing → all pending_time."""
    log = patched_stores["log"]
    _seed_predictions(log, n=2)

    r = await client.get("/models/gbm_predictor/outcomes", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    for o in body["outcomes"]:
        assert o["settlement_status"] == "pending_time"


# --------------------------------------------------------------------------- #
# Pagination by since_ns                                                      #
# --------------------------------------------------------------------------- #


async def test_outcomes_since_ns_filters_predictions(
    client: AsyncClient,
    auth_headers: dict[str, str],
    patched_stores,
) -> None:
    """since_ns filters the prediction-log read (by ts_recorded)."""
    log = patched_stores["log"]
    _seed_predictions(log, n=5)

    # Read all first to confirm the route works.
    r_all = await client.get("/models/gbm_predictor/outcomes", headers=auth_headers)
    assert r_all.status_code == 200
    # ts_recorded is not in the outcome shape, but we can read it
    # from the log directly to find a cutoff.
    all_preds = log.read(
        agent_id="gbm_predictor.v1", model_name="gbm_predictor", limit=100
    )
    # Sort newest-first (read already does this); pick the 3rd newest.
    cutoff = all_preds[2].ts_recorded

    r = await client.get(
        f"/models/gbm_predictor/outcomes?since_ns={cutoff}",
        headers=auth_headers,
    )
    assert r.status_code == 200
    body = r.json()
    # The filter keeps rows with ts_recorded >= cutoff.  On platforms
    # with coarse clock granularity (e.g. Windows ~15ms), predictions
    # written in a tight loop can share the same ts_recorded value, so
    # the exact count depends on how many ties exist at the cutoff.
    # Assert the route returns exactly the rows the filter should keep.
    expected = sum(1 for p in all_preds if p.ts_recorded >= cutoff)
    assert body["count"] == expected
    assert body["count"] >= 1


# --------------------------------------------------------------------------- #
# Missing data files                                                          #
# --------------------------------------------------------------------------- #


async def test_outcomes_missing_prediction_file_returns_empty(
    client: AsyncClient,
    auth_headers: dict[str, str],
    patched_stores,
) -> None:
    """No predictions JSONL → empty list, no crash."""
    r = await client.get("/models/gbm_predictor/outcomes", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["outcomes"] == []


async def test_outcomes_missing_settlement_file_returns_pending(
    client: AsyncClient,
    auth_headers: dict[str, str],
    patched_stores,
) -> None:
    """Predictions exist but no settlement JSONL → all pending_time."""
    log = patched_stores["log"]
    _seed_predictions(log, n=1)

    r = await client.get("/models/gbm_predictor/outcomes", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["outcomes"][0]["settlement_status"] == "pending_time"


# --------------------------------------------------------------------------- #
# Malformed settlement JSONL skipped                                          #
# --------------------------------------------------------------------------- #


async def test_outcomes_malformed_settlement_line_skipped(
    client: AsyncClient,
    auth_headers: dict[str, str],
    patched_stores,
) -> None:
    """A corrupt line in the settlement JSONL is skipped, not a 500."""
    log = patched_stores["log"]
    store = patched_stores["settlements"]

    preds = _seed_predictions(log, n=2)
    store.append(_make_settlement(preds[0]))

    # Append a malformed line directly to the settlement file.
    settlement_path = store.root / "gbm_predictor.v1.jsonl"
    with settlement_path.open("a", encoding="utf-8") as f:
        f.write("{NOT VALID JSON}\n")

    r = await client.get("/models/gbm_predictor/outcomes", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    # The valid settlement still joins; the malformed line is skipped.
    statuses = {o["prediction_id"]: o["settlement_status"] for o in body["outcomes"]}
    assert statuses[preds[0].id] == "settled"
    assert statuses[preds[1].id] == "pending_time"


# --------------------------------------------------------------------------- #
# Limit bounds                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad_limit", [0, -1, 1001])
async def test_outcomes_rejects_out_of_range_limit(
    client: AsyncClient,
    auth_headers: dict[str, str],
    patched_stores,
    bad_limit: int,
) -> None:
    r = await client.get(
        f"/models/gbm_predictor/outcomes?limit={bad_limit}",
        headers=auth_headers,
    )
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Feature snapshots                                                           #
# --------------------------------------------------------------------------- #


async def test_outcomes_includes_feature_schema_hash_when_snapshot_exists(
    client: AsyncClient,
    auth_headers: dict[str, str],
    patched_stores,
) -> None:
    """When a feature snapshot is recorded for a prediction, the outcome
    carries the snapshot's ``feature_schema_hash``."""
    from fincept_core.datasets import FeatureRow, FeatureSnapshot

    log = patched_stores["log"]
    snapshot_store = patched_stores["snapshots"]

    preds = _seed_predictions(log, n=2)
    # Record a snapshot only for the first prediction.
    schemash = "b" * 64
    snapshot = FeatureSnapshot(
        decision_time_ns=preds[0].ts_event,
        rows=[
            FeatureRow(
                symbol=preds[0].symbol,
                ts=preds[0].ts_event,
                features={"f1": 1.0},
            )
        ],
        feature_schema_hash=schemash,
    )
    snapshot_store.append_if_missing(
        preds[0].id,
        snapshot,
        agent_id="gbm_predictor.v1",
    )

    r = await client.get("/models/gbm_predictor/outcomes", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2

    by_pid = {o["prediction_id"]: o for o in body["outcomes"]}
    # The prediction with a snapshot carries the schema hash.
    assert by_pid[preds[0].id]["feature_schema_hash"] == schemash
    # The prediction without a snapshot does not carry the key.
    assert "feature_schema_hash" not in by_pid[preds[1].id]


async def test_outcomes_no_snapshot_key_when_absent(
    client: AsyncClient,
    auth_headers: dict[str, str],
    patched_stores,
) -> None:
    """When no feature snapshot exists for any prediction, the
    ``feature_schema_hash`` key is absent from every outcome."""
    log = patched_stores["log"]
    _seed_predictions(log, n=3)

    r = await client.get("/models/gbm_predictor/outcomes", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    for o in body["outcomes"]:
        assert "feature_schema_hash" not in o
