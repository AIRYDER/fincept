"""
Tests for features.store.

OnlineStore tests use ``fakeredis.aioredis.FakeRedis`` so they execute
in-process without a real Redis.  OfflineStore tests inject in-memory
fakes for ``write_fn`` / ``read_fn`` so they're independent of the
database — DB-backed integration is covered by ``libs/fincept-db/tests``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import fakeredis.aioredis
import pytest

from features.store import OfflineStore, OnlineStore
from fincept_core.schemas import FeatureFrame


def _frame(symbol: str = "BTC-USD", ts: int = 1_000, freq: str = "1m") -> FeatureFrame:
    return FeatureFrame(
        symbol=symbol,
        ts_event=ts,
        freq=freq,
        values={"ret_log_1": 0.01, "vol_rs_20": None},
        tags={"runner": "test"},
    )


# ---------------------------------------------------------------------------
# OnlineStore — fakeredis-backed
# ---------------------------------------------------------------------------


@pytest.fixture
def online_store() -> OnlineStore:
    return OnlineStore(fakeredis.aioredis.FakeRedis())


async def test_online_store_round_trips_a_frame(online_store: OnlineStore) -> None:
    frame = _frame()
    await online_store.put(frame)
    out = await online_store.get_latest("BTC-USD", "1m")
    assert out is not None
    assert out == frame


async def test_online_store_returns_none_when_key_missing(
    online_store: OnlineStore,
) -> None:
    out = await online_store.get_latest("MSFT", "1d")
    assert out is None


async def test_online_store_overwrites_on_repeat_put(online_store: OnlineStore) -> None:
    """The cache is last-write-wins — a fresher frame replaces stale state."""
    await online_store.put(_frame(ts=1_000))
    await online_store.put(_frame(ts=2_000))
    out = await online_store.get_latest("BTC-USD", "1m")
    assert out is not None
    assert out.ts_event == 2_000


async def test_online_store_partitions_by_freq(online_store: OnlineStore) -> None:
    """Same symbol, different freqs -> independent cache slots."""
    await online_store.put(_frame(freq="1m", ts=1_000))
    await online_store.put(_frame(freq="1h", ts=2_000))
    minute = await online_store.get_latest("BTC-USD", "1m")
    hour = await online_store.get_latest("BTC-USD", "1h")
    assert minute is not None and minute.ts_event == 1_000
    assert hour is not None and hour.ts_event == 2_000


# ---------------------------------------------------------------------------
# OfflineStore — pure DI fakes
# ---------------------------------------------------------------------------


class _FakeDb:
    """In-memory write_fn/read_fn pair satisfying the OfflineStore protocols."""

    def __init__(self) -> None:
        self.frames: dict[tuple[str, str, int], FeatureFrame] = {}

    async def write(self, frames: Iterable[FeatureFrame]) -> int:
        n = 0
        for f in frames:
            self.frames[(f.symbol, f.freq, f.ts_event)] = f
            n += 1
        return n

    async def read(self, symbol: str, freq: str, start_ns: int, end_ns: int) -> list[FeatureFrame]:
        return sorted(
            (
                f
                for (s, fr, ts), f in self.frames.items()
                if s == symbol and fr == freq and start_ns <= ts < end_ns
            ),
            key=lambda f: f.ts_event,
        )


async def test_offline_store_put_many_returns_row_count() -> None:
    db = _FakeDb()
    store = OfflineStore(write_fn=db.write, read_fn=db.read)

    n = await store.put_many([_frame(ts=1_000), _frame(ts=2_000)])
    assert n == 2
    assert len(db.frames) == 2


async def test_offline_store_read_range_filters_correctly() -> None:
    db = _FakeDb()
    store = OfflineStore(write_fn=db.write, read_fn=db.read)
    await store.put_many([_frame(ts=ts) for ts in (500, 1_500, 2_500)])

    out = await store.read_range("BTC-USD", "1m", start_ns=1_000, end_ns=2_000)
    assert [f.ts_event for f in out] == [1_500]


async def test_offline_store_default_wires_to_fincept_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without injected fakes, OfflineStore reaches for fincept_db.features."""
    captured: dict[str, Any] = {}

    async def fake_write(frames: Iterable[FeatureFrame]) -> int:
        captured["wrote"] = list(frames)
        return len(captured["wrote"])

    async def fake_read(symbol: str, freq: str, start: int, end: int) -> list[FeatureFrame]:
        captured["read"] = (symbol, freq, start, end)
        return []

    monkeypatch.setattr("features.store.write_features", fake_write)
    monkeypatch.setattr("features.store.read_features", fake_read)
    store = OfflineStore()  # no DI overrides

    await store.put_many([_frame(ts=1)])
    await store.read_range("BTC-USD", "1m", 0, 100)
    assert "wrote" in captured
    assert captured["read"] == ("BTC-USD", "1m", 0, 100)
