"""DB-gated round-trip tests for fincept_db.features."""

from __future__ import annotations

import pytest

from fincept_core.schemas import FeatureFrame
from fincept_db.features import read_features, read_latest_feature, write_features


def _frame(
    ts: int, *, symbol: str = "BTC-USD", freq: str = "1m", **values: float | None
) -> FeatureFrame:
    return FeatureFrame(
        symbol=symbol,
        ts_event=ts,
        freq=freq,
        values=values or {"ret_log_1": 0.01, "vol_rs_20": None},
        tags={"runner": "test"},
    )


@pytest.mark.asyncio
async def test_write_and_read_features_roundtrip() -> None:
    frames = [_frame(ts) for ts in (1_000, 2_000, 3_000)]
    written = await write_features(frames)
    assert written == 3

    out = await read_features("BTC-USD", "1m", 0, 4_000)
    assert [f.ts_event for f in out] == [1_000, 2_000, 3_000]
    assert out[0].values == {"ret_log_1": 0.01, "vol_rs_20": None}
    assert out[0].tags == {"runner": "test"}


@pytest.mark.asyncio
async def test_write_features_upserts_on_primary_key() -> None:
    """Re-running backfill must replace prior values for the same key."""
    await write_features([_frame(ts=500, ret_log_1=0.01)])
    await write_features([_frame(ts=500, ret_log_1=0.05)])

    out = await read_features("BTC-USD", "1m", 0, 1_000)
    assert len(out) == 1
    assert out[0].values["ret_log_1"] == 0.05


@pytest.mark.asyncio
async def test_read_features_filters_by_freq() -> None:
    await write_features([_frame(ts=100, freq="1m"), _frame(ts=200, freq="1h")])

    minute = await read_features("BTC-USD", "1m", 0, 1_000)
    hour = await read_features("BTC-USD", "1h", 0, 1_000)

    assert [f.ts_event for f in minute] == [100]
    assert [f.ts_event for f in hour] == [200]


@pytest.mark.asyncio
async def test_read_latest_feature_respects_as_of_ns() -> None:
    """PIT semantics: as_of_ns=15 sees the t=10 frame, not t=20."""
    await write_features(
        [
            _frame(ts=10, ret_log_1=0.01),
            _frame(ts=20, ret_log_1=0.02),
        ]
    )
    latest = await read_latest_feature("BTC-USD", "1m", as_of_ns=15)
    assert latest is not None
    assert latest.ts_event == 10
    assert latest.values["ret_log_1"] == 0.01


@pytest.mark.asyncio
async def test_read_latest_feature_returns_none_when_no_history() -> None:
    latest = await read_latest_feature("BTC-USD", "1m", as_of_ns=99)
    assert latest is None


@pytest.mark.asyncio
async def test_read_latest_feature_inclusive_at_exact_match() -> None:
    """as_of_ns == ts_event must return that frame (closed interval on the upper end)."""
    await write_features([_frame(ts=10, ret_log_1=0.01)])
    latest = await read_latest_feature("BTC-USD", "1m", as_of_ns=10)
    assert latest is not None
    assert latest.ts_event == 10
