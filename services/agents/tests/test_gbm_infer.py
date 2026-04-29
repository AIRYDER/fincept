"""End-to-end test: train -> save -> GBMPredictor.setup -> _predict."""

from __future__ import annotations

import json
import pathlib
from collections.abc import AsyncIterator
from typing import Any

import fakeredis.aioredis
import numpy as np
import pytest
import pytest_asyncio
from redis.asyncio import Redis

from features.store import OnlineStore
from fincept_core.schemas import FeatureFrame, Prediction

from agents.gbm_predictor.features import FEATURES
from agents.gbm_predictor.infer import GBMPredictor
from agents.gbm_predictor.train import save_artifacts, train_booster


@pytest.fixture
def model_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Train a tiny lightgbm model and write artifacts.  Reused across
    tests via session-scope-ish caching is unnecessary; fitting 200x10
    takes ~50ms."""
    rng = np.random.default_rng(0)
    n_features = len(FEATURES)
    X = rng.normal(0, 1, (300, n_features))
    # Easy linear signal: feature index 0 dominates.
    y = (X[:, 0] > 0).astype(int)
    model, meta = train_booster(
        X, y, num_boost_round=30, early_stopping_rounds=10, val_fraction=0.2
    )
    out = tmp_path / "model_out"
    save_artifacts(
        model,
        out_dir=out,
        feature_names=FEATURES,
        horizon_bars=15,
        bar_seconds=60,
        extra_meta=meta,
    )
    return out


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis[Any]]:
    client = fakeredis.aioredis.FakeRedis()
    try:
        yield client
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# setup()
# ---------------------------------------------------------------------------


async def test_setup_loads_model_and_meta(
    model_dir: pathlib.Path, redis: Redis[Any]
) -> None:
    agent = GBMPredictor(model_dir=model_dir, redis=redis)
    await agent.setup()
    # After setup the agent has a Booster and the feature list from meta.
    assert agent._model is not None  # type: ignore[has-attr]
    assert agent._features == FEATURES  # type: ignore[has-attr]
    assert agent._horizon_ns == 15 * 60 * 1_000_000_000  # type: ignore[has-attr]


async def test_setup_raises_when_artifacts_missing(
    tmp_path: pathlib.Path, redis: Redis[Any]
) -> None:
    agent = GBMPredictor(model_dir=tmp_path / "does_not_exist", redis=redis)
    with pytest.raises(FileNotFoundError, match="missing"):
        await agent.setup()


async def test_setup_uses_feature_list_from_meta(
    model_dir: pathlib.Path, redis: Redis[Any]
) -> None:
    """If meta.json carries a different feature order, the agent
    respects it.  This is what protects the train/inference contract."""
    # Rewrite meta.json with a permuted order.
    meta_path = model_dir / "meta.json"
    meta = json.loads(meta_path.read_text())
    permuted = list(reversed(meta["features"]))
    meta["features"] = permuted
    meta_path.write_text(json.dumps(meta))

    agent = GBMPredictor(model_dir=model_dir, redis=redis)
    await agent.setup()
    assert agent._features == permuted  # type: ignore[has-attr]


# ---------------------------------------------------------------------------
# _predict()
# ---------------------------------------------------------------------------


async def test_predict_emits_prediction_with_canonical_shape(
    model_dir: pathlib.Path, redis: Redis[Any]
) -> None:
    agent = GBMPredictor(model_dir=model_dir, redis=redis)
    await agent.setup()

    row = dict.fromkeys(FEATURES, 0.5)
    pred = agent._predict("BTC-USD", row)  # type: ignore[arg-type]
    assert isinstance(pred, Prediction)
    assert pred.agent_id == "gbm_predictor.v1"
    assert pred.symbol == "BTC-USD"
    assert pred.horizon_ns == 15 * 60 * 1_000_000_000
    assert -1.0 <= pred.direction <= 1.0
    assert 0.0 <= pred.confidence <= 1.0
    assert pred.calibration_tag == "gbm.v1"


async def test_predict_direction_correlates_with_first_feature(
    model_dir: pathlib.Path, redis: Redis[Any]
) -> None:
    """Our synthetic training set has y=1 iff feature 0 > 0.  A trained
    model should map a positive feature 0 to direction > 0 (most of the
    time).  This sanity-checks the direction calibration formula
    (direction = 2*p - 1)."""
    agent = GBMPredictor(model_dir=model_dir, redis=redis)
    await agent.setup()

    row_up = {f: 0.0 for f in FEATURES}
    row_up[FEATURES[0]] = 2.0  # strong positive on the dominant feature
    row_down = {f: 0.0 for f in FEATURES}
    row_down[FEATURES[0]] = -2.0

    pred_up = agent._predict("BTC-USD", row_up)  # type: ignore[arg-type]
    pred_down = agent._predict("BTC-USD", row_down)  # type: ignore[arg-type]
    assert pred_up.direction > pred_down.direction


# ---------------------------------------------------------------------------
# run() -> integration
# ---------------------------------------------------------------------------


async def test_run_yields_predictions_for_warm_symbols_and_skips_cold(
    model_dir: pathlib.Path, redis: Redis[Any]
) -> None:
    """Two universe symbols, only one has fresh features in OnlineStore."""
    store = OnlineStore(redis)
    warm_values = dict.fromkeys(FEATURES, 0.5)
    await store.put(
        FeatureFrame(symbol="BTC-USD", ts_event=1, freq="1m", values=warm_values)
    )
    # ETH-USD intentionally not seeded.

    agent = GBMPredictor(
        model_dir=model_dir,
        redis=redis,
        cadence_s=60.0,  # high; we exit after the first cycle
        symbols=["BTC-USD", "ETH-USD"],
    )
    await agent.setup()

    # Pull only one cycle's worth of yields.  We can't easily limit
    # cadence sleep without monkeypatching, so we just collect all
    # events emitted before the first sleep.
    seen: list[Prediction] = []
    gen = agent.run()
    # Advance until either we have collected expected count or the
    # generator awaits sleep (we trust load_live is fast).
    for _ in range(2):
        try:
            event = await anext(gen)
        except StopAsyncIteration:
            break
        if isinstance(event, Prediction):
            seen.append(event)
        # break before the cadence sleep would block us
        if len(seen) >= 1:
            break
    await agent.teardown()

    assert len(seen) == 1
    assert seen[0].symbol == "BTC-USD"


async def test_run_before_setup_raises(
    model_dir: pathlib.Path, redis: Redis[Any]
) -> None:
    agent = GBMPredictor(model_dir=model_dir, redis=redis)
    with pytest.raises(RuntimeError, match="setup"):
        async for _ in agent.run():
            break
