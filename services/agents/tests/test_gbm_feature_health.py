"""Tests for the feature-availability JSONL sidecar (todo 9).

Covers two layers:

  1. ``agents.gbm_predictor.features.load_live`` -- the new
     :class:`FeatureHealth` return value (missing / defaulted / aliased
     diagnostics).
  2. ``agents.gbm_predictor.main.FeatureHealthLog`` + the publish
     loop integration -- the sidecar JSONL row written alongside each
     prediction, and the best-effort contract that a write failure
     never crashes inference.

The acceptance criteria from the plan call for >= 4 tests:
round-trip, defaulted features recorded, missing features recorded,
write failure does not crash inference.  This file provides those
plus an aliased-features test and a publish-loop integration test.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import AsyncIterator
from typing import Any

import fakeredis.aioredis
import pytest
import pytest_asyncio
from redis.asyncio import Redis

from features.store import OnlineStore
from fincept_core.prediction_log import PredictionLog
from fincept_core.schemas import FeatureFrame, Prediction

from agents.gbm_predictor import main as gbm_main
from agents.gbm_predictor.features import FEATURES, FeatureHealth, load_live


# --------------------------------------------------------------------------- #
# Shared fixtures                                                             #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis[Any]]:
    client = fakeredis.aioredis.FakeRedis()
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def store(redis: Redis[Any]) -> OnlineStore:
    return OnlineStore(redis)


def _frame(symbol: str = "BTC-USD", **values: float | None) -> FeatureFrame:
    return FeatureFrame(symbol=symbol, ts_event=1_000, freq="1m", values=values)


# --------------------------------------------------------------------------- #
# load_live -> FeatureHealth diagnostics                                      #
# --------------------------------------------------------------------------- #


async def test_load_live_records_missing_feature(store: OnlineStore) -> None:
    """A defaultable feature absent from the online frame is recorded in
    ``missing`` (and ``defaulted``) while still producing a feature vector."""
    # Provide every feature EXCEPT mom_z_240m.
    values: dict[str, float | None] = dict.fromkeys(FEATURES, 1.0)
    values.pop("mom_z_240m")
    await store.put(_frame(**values))

    result = await load_live(
        store,
        "BTC-USD",
        feature_names=FEATURES,
        allow_compat_defaults=True,
    )
    assert result is not None
    features, health = result
    # The feature was filled with the 0.0 compat default.
    assert features["mom_z_240m"] == 0.0
    # And recorded as missing from the online frame.
    assert "mom_z_240m" in health.missing
    assert "mom_z_240m" in health.defaulted


async def test_load_live_records_defaulted_features(store: OnlineStore) -> None:
    """When only the strict feature (ret_1m) is present, every other
    defaultable feature falls back to 0.0 and is recorded in
    ``defaulted``."""
    await store.put(_frame(ret_1m=0.01))

    result = await load_live(
        store,
        "BTC-USD",
        feature_names=FEATURES,
        allow_compat_defaults=True,
    )
    assert result is not None
    _features, health = result
    # ret_1m is strict (not defaultable, not aliased here because we
    # provided the canonical name) -> not in any diagnostic list.
    assert "ret_1m" not in health.defaulted
    assert "ret_1m" not in health.missing
    # Every other feature is defaultable and was absent -> defaulted.
    expected_defaulted = set(FEATURES) - {"ret_1m"}
    assert set(health.defaulted) == expected_defaulted
    # All of them were also missing from the frame.
    assert set(health.missing) == expected_defaulted


async def test_load_live_records_aliased_features(store: OnlineStore) -> None:
    """A feature resolved via FEATURE_ALIASES is recorded in
    ``aliased`` and NOT in ``missing`` (the data exists, just under a
    legacy name)."""
    # Provide only the alias name for ret_1m.
    await store.put(_frame(ret_simple_1=0.42))

    result = await load_live(
        store,
        "BTC-USD",
        feature_names=FEATURES,
        allow_compat_defaults=True,
    )
    assert result is not None
    features, health = result
    assert features["ret_1m"] == 0.42
    assert health.aliased == ["ret_1m"]
    # Aliased features are recovered -> not missing.
    assert "ret_1m" not in health.missing


async def test_load_live_no_diagnostics_when_all_canonical(store: OnlineStore) -> None:
    """All features present under canonical names -> empty health lists."""
    await store.put(_frame(**dict.fromkeys(FEATURES, 1.5)))

    result = await load_live(
        store,
        "BTC-USD",
        feature_names=FEATURES,
        allow_compat_defaults=True,
    )
    assert result is not None
    _features, health = result
    assert health.missing == []
    assert health.defaulted == []
    assert health.aliased == []


# --------------------------------------------------------------------------- #
# FeatureHealthLog round-trip                                                 #
# --------------------------------------------------------------------------- #


def test_feature_health_log_round_trip(tmp_path: pathlib.Path) -> None:
    """append -> read returns the same row, JSONL line is valid JSON."""
    health_dir = tmp_path / "feature_health"
    fh_log = gbm_main.FeatureHealthLog(health_dir=health_dir)

    fh_log.append(
        agent_id="gbm_predictor.v1",
        prediction_id="abc123",
        ts_event=1_700_000_000_000_000_000,
        symbol="BTC-USD",
        missing=["mom_z_240m"],
        defaulted=["mom_z_240m"],
        aliased=["ret_1m"],
    )

    path = health_dir / "gbm_predictor.v1.jsonl"
    assert path.is_file()
    line = path.read_text(encoding="utf-8").strip()
    data = json.loads(line)
    assert set(data.keys()) == {
        "prediction_id",
        "ts_event",
        "symbol",
        "missing",
        "defaulted",
        "aliased",
    }
    assert data["missing"] == ["mom_z_240m"]
    assert data["defaulted"] == ["mom_z_240m"]
    assert data["aliased"] == ["ret_1m"]

    rows = fh_log.read(agent_id="gbm_predictor.v1")
    assert len(rows) == 1
    row = rows[0]
    assert row.prediction_id == "abc123"
    assert row.symbol == "BTC-USD"
    assert row.missing == ["mom_z_240m"]
    assert row.defaulted == ["mom_z_240m"]
    assert row.aliased == ["ret_1m"]


def test_feature_health_log_rejects_bad_agent_id(tmp_path: pathlib.Path) -> None:
    """_validate_agent_id (shared with prediction_log) is enforced."""
    fh_log = gbm_main.FeatureHealthLog(health_dir=tmp_path / "fh")
    with pytest.raises(ValueError):
        fh_log.append(
            agent_id="../escape",
            prediction_id="abc",
            ts_event=1,
            symbol="BTC-USD",
            missing=[],
            defaulted=[],
            aliased=[],
        )


# --------------------------------------------------------------------------- #
# Publish-loop integration                                                    #
# --------------------------------------------------------------------------- #


class _FakeAgent:
    """Stand-in for GBMPredictor that yields one Prediction then stops.

    Exposes ``last_feature_health`` (the attribute the publish loop
    reads) so we can drive the sidecar write without a real model.
    """

    agent_id = "gbm_predictor.v1"

    def __init__(self, health: FeatureHealth) -> None:
        self.last_feature_health = health

    async def run(self) -> AsyncIterator[Prediction]:
        yield Prediction(
            agent_id=self.agent_id,
            symbol="BTC-USD",
            horizon_ns=60_000_000_000,
            ts_event=1_700_000_000_000_000_000,
            direction=0.5,
            confidence=0.5,
            calibration_tag="gbm.v1",
        )
        # Stop after one yield so _publish_loop's `async for` exits.


class _RecordingProducer:
    """Captures publish calls; stands in for fincept_bus.Producer."""

    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, stream: str, event: Any) -> None:
        self.published.append((stream, event))


async def test_publish_loop_writes_feature_health_sidecar(
    tmp_path: pathlib.Path,
) -> None:
    """Happy path: one prediction -> one FeatureHealthRow on disk, joined
    by prediction_id to the PredictionRow written by prediction_log."""
    agent = _FakeAgent(
        FeatureHealth(missing=["mom_z_240m"], defaulted=["mom_z_240m"], aliased=[])
    )
    producer = _RecordingProducer()
    prediction_log = PredictionLog(predictions_dir=tmp_path / "predictions")
    fh_log = gbm_main.FeatureHealthLog(health_dir=tmp_path / "feature_health")

    await gbm_main._publish_loop(
        agent,  # type: ignore[arg-type]
        producer,  # type: ignore[arg-type]
        prediction_log=prediction_log,
        feature_health_log=fh_log,
        model_name="gbm_predictor",
    )

    # Prediction was published.
    assert len(producer.published) == 1
    # Prediction row was recorded.
    pred_rows = prediction_log.read(agent_id="gbm_predictor.v1")
    assert len(pred_rows) == 1
    # Feature health sidecar was recorded and joins by prediction_id.
    fh_rows = fh_log.read(agent_id="gbm_predictor.v1")
    assert len(fh_rows) == 1
    assert fh_rows[0].prediction_id == pred_rows[0].id
    assert fh_rows[0].symbol == "BTC-USD"
    assert fh_rows[0].missing == ["mom_z_240m"]
    assert fh_rows[0].defaulted == ["mom_z_240m"]
    assert fh_rows[0].aliased == []


async def test_publish_loop_skips_sidecar_when_no_health(tmp_path: pathlib.Path) -> None:
    """If the agent exposes no last_feature_health, no sidecar row is
    written but the prediction is still published + recorded."""
    agent = _FakeAgent(FeatureHealth(missing=[], defaulted=[], aliased=[]))
    # Simulate "no health available" by clearing the attribute.
    agent.last_feature_health = None  # type: ignore[assignment]
    producer = _RecordingProducer()
    prediction_log = PredictionLog(predictions_dir=tmp_path / "predictions")
    fh_log = gbm_main.FeatureHealthLog(health_dir=tmp_path / "feature_health")

    await gbm_main._publish_loop(
        agent,  # type: ignore[arg-type]
        producer,  # type: ignore[arg-type]
        prediction_log=prediction_log,
        feature_health_log=fh_log,
        model_name="gbm_predictor",
    )

    assert len(producer.published) == 1
    assert prediction_log.read(agent_id="gbm_predictor.v1")
    assert fh_log.read(agent_id="gbm_predictor.v1") == []


async def test_feature_health_write_failure_does_not_crash_inference(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure path: when the health sidecar write raises, the publish
    loop logs ``feature_health_write_failed`` and still publishes +
    records the prediction (inference is not broken)."""
    agent = _FakeAgent(
        FeatureHealth(missing=["mom_z_240m"], defaulted=["mom_z_240m"], aliased=[])
    )
    producer = _RecordingProducer()
    prediction_log = PredictionLog(predictions_dir=tmp_path / "predictions")

    # Point the health log at a path whose parent is a FILE, so the
    # mkdir inside append raises.  This simulates an unwritable health
    # dir without OS-specific chmod gymnastics.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    fh_log = gbm_main.FeatureHealthLog(health_dir=blocker)

    # structlog does not propagate to stdlib caplog, so capture
    # warning calls directly off the module logger.
    warnings: list[tuple[str, dict[str, Any]]] = []
    orig_warning = gbm_main.log.warning

    def fake_warning(event: str, **kw: Any) -> None:
        warnings.append((event, kw))
        orig_warning(event, **kw)

    monkeypatch.setattr(gbm_main.log, "warning", fake_warning)

    await gbm_main._publish_loop(
        agent,  # type: ignore[arg-type]
        producer,  # type: ignore[arg-type]
        prediction_log=prediction_log,
        feature_health_log=fh_log,
        model_name="gbm_predictor",
    )

    # Inference was NOT broken: prediction published + recorded.
    assert len(producer.published) == 1
    assert prediction_log.read(agent_id="gbm_predictor.v1")
    # The failure was logged as feature_health_write_failed.
    assert any(event == "feature_health_write_failed" for event, _ in warnings), (
        f"expected feature_health_write_failed warning, got: {warnings}"
    )
