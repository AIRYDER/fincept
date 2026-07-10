"""Tests for agents.gbm_predictor.features.load_live."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import fakeredis.aioredis
import pytest_asyncio
from features.store import OnlineStore
from fincept_core.schemas import FeatureFrame
from redis.asyncio import Redis

from agents.gbm_predictor.features import FEATURES, load_live


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


# ---------------------------------------------------------------------------
# Cache miss
# ---------------------------------------------------------------------------


async def test_returns_none_when_no_frame_cached(store: OnlineStore) -> None:
    result = await load_live(store, "BTC-USD", feature_names=FEATURES)
    assert result is None


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_returns_dict_when_all_features_present(store: OnlineStore) -> None:
    values = dict.fromkeys(FEATURES, 1.5)
    await store.put(_frame(**values))

    result = await load_live(store, "BTC-USD", feature_names=FEATURES)
    assert result is not None
    features, health = result
    assert set(features.keys()) == set(FEATURES)
    for name in FEATURES:
        assert features[name] == 1.5
    # All features present under their canonical names -> no diagnostics.
    assert health.missing == []
    assert health.defaulted == []
    assert health.aliased == []


async def test_projects_only_requested_feature_names(store: OnlineStore) -> None:
    """If we ask for a subset, we get only that subset (and the frame
    can have extras that are ignored)."""
    extras = dict.fromkeys(FEATURES, 1.0)
    extras["unrelated_feature"] = 999.0
    await store.put(_frame(**extras))

    subset = ["ret_1m", "ret_5m"]
    result = await load_live(store, "BTC-USD", feature_names=subset)
    assert result is not None
    features, _ = result
    assert set(features.keys()) == set(subset)


# ---------------------------------------------------------------------------
# Missing or null feature -> None
# ---------------------------------------------------------------------------


async def test_returns_none_when_required_feature_missing(store: OnlineStore) -> None:
    values: dict[str, float | None] = dict.fromkeys(FEATURES, 1.0)
    values.pop("ret_5m")  # one feature missing entirely
    await store.put(_frame(**values))

    result = await load_live(store, "BTC-USD", feature_names=FEATURES)
    assert result is None


async def test_returns_none_when_required_feature_is_null(store: OnlineStore) -> None:
    """Pydantic stores explicit None values; load_live treats them as
    'not yet warm' and returns None."""
    values: dict[str, float | None] = dict.fromkeys(FEATURES, 1.0)
    values["spread_bps"] = None
    await store.put(_frame(**values))

    result = await load_live(store, "BTC-USD", feature_names=FEATURES)
    assert result is None


async def test_compat_mode_projects_current_live_feature_names(
    store: OnlineStore,
) -> None:
    await store.put(
        _frame(
            ret_simple_1=0.01,
        )
    )

    result = await load_live(
        store,
        "BTC-USD",
        feature_names=FEATURES,
        allow_compat_defaults=True,
    )

    assert result is not None
    features, health = result
    assert features["ret_1m"] == 0.01
    assert features["ret_5m"] == 0.0
    assert features["ret_15m"] == 0.0
    assert features["ret_60m"] == 0.0
    assert features["rv_5m"] == 0.0
    assert features["rv_30m"] == 0.0
    assert features["mom_z_30m"] == 0.0
    assert features["mom_z_240m"] == 0.0
    assert features["book_imbalance_1"] == 0.0
    assert features["spread_bps"] == 0.0
    # ret_1m was resolved via alias (ret_simple_1); the rest were absent
    # and fell back to the 0.0 default.
    assert health.aliased == ["ret_1m"]
    assert "ret_1m" not in health.missing


# ---------------------------------------------------------------------------
# Freq dimension
# ---------------------------------------------------------------------------


async def test_freq_argument_disambiguates_cache_keys(store: OnlineStore) -> None:
    values = dict.fromkeys(FEATURES, 1.0)
    await store.put(
        FeatureFrame(symbol="BTC-USD", ts_event=1, freq="5m", values=values)
    )
    # Default freq is 1m; frame is at 5m -> miss.
    assert await load_live(store, "BTC-USD", feature_names=FEATURES) is None
    # Explicit freq=5m -> hit.
    result = await load_live(store, "BTC-USD", feature_names=FEATURES, freq="5m")
    assert result is not None
