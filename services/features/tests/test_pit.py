"""
Tests for features.pit.PITJoiner.

The headline test is ``test_join_never_returns_a_future_feature`` —
the no-lookahead invariant.  If that ever fails, every backtest run on
this codebase is suspect.

All tests use an injected fake OfflineStore so they're DB-free and fast.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

import pytest

from features.pit import PITJoiner
from features.store import OfflineStore
from fincept_core.schemas import AssetClass, BarEvent, FeatureFrame, Venue


class _FakeDb:
    def __init__(self) -> None:
        self.frames: list[FeatureFrame] = []

    async def write(self, frames: Iterable[FeatureFrame]) -> int:
        self.frames.extend(frames)
        return len(self.frames)

    async def read(self, symbol: str, freq: str, start_ns: int, end_ns: int) -> list[FeatureFrame]:
        return sorted(
            (
                f
                for f in self.frames
                if f.symbol == symbol and f.freq == freq and start_ns <= f.ts_event < end_ns
            ),
            key=lambda f: f.ts_event,
        )


def _bar(symbol: str, ts: int, *, freq: str = "1m") -> BarEvent:
    p = Decimal("100")
    return BarEvent(
        venue=Venue.BINANCE,
        symbol=symbol,
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=ts,
        ts_recv=ts,
        freq=freq,
        open=p,
        high=p,
        low=p,
        close=p,
        volume=Decimal("0"),
        trades=0,
    )


def _frame(symbol: str, ts: int, *, freq: str = "1m", value: float = 1.0) -> FeatureFrame:
    return FeatureFrame(
        symbol=symbol,
        ts_event=ts,
        freq=freq,
        values={"x": value},
    )


@pytest.fixture
def store_with_frames() -> tuple[OfflineStore, _FakeDb]:
    db = _FakeDb()
    store = OfflineStore(write_fn=db.write, read_fn=db.read)
    return store, db


# ---------------------------------------------------------------------------
# Core invariant
# ---------------------------------------------------------------------------


async def test_join_never_returns_a_future_feature(
    store_with_frames: tuple[OfflineStore, _FakeDb],
) -> None:
    """The headline PIT invariant: a bar at T must see only features at T or earlier."""
    store, _db = store_with_frames
    await store.put_many([_frame("X", ts=10, value=1.0), _frame("X", ts=20, value=2.0)])
    joiner = PITJoiner(store, lookback_ns=1_000_000)

    # bar at t=15 must see the t=10 feature, NOT the t=20 feature.
    out = await joiner.join_bars([_bar("X", ts=15)])

    assert len(out) == 1
    bar, feat = out[0]
    assert bar.ts_event == 15
    assert feat is not None
    assert feat.ts_event == 10
    assert feat.values["x"] == 1.0


async def test_bar_at_exact_feature_ts_sees_that_feature(
    store_with_frames: tuple[OfflineStore, _FakeDb],
) -> None:
    """``feature.ts_event == bar.ts_event`` is allowed — it's the bar's own feature."""
    store, _ = store_with_frames
    await store.put_many([_frame("X", ts=10, value=1.0), _frame("X", ts=20, value=2.0)])
    joiner = PITJoiner(store, lookback_ns=1_000_000)

    out = await joiner.join_bars([_bar("X", ts=20)])
    _bar_out, feat = out[0]
    assert feat is not None
    assert feat.ts_event == 20  # the t=20 feature is in-time, not future
    assert feat.values["x"] == 2.0


async def test_bar_before_any_feature_returns_none(
    store_with_frames: tuple[OfflineStore, _FakeDb],
) -> None:
    """No history -> no feature.  Consumers handle None; never default to 0."""
    store, _ = store_with_frames
    await store.put_many([_frame("X", ts=100, value=1.0)])
    joiner = PITJoiner(store, lookback_ns=1_000_000)

    out = await joiner.join_bars([_bar("X", ts=50)])
    bar, feat = out[0]
    assert bar.ts_event == 50
    assert feat is None


# ---------------------------------------------------------------------------
# Multi-symbol grouping
# ---------------------------------------------------------------------------


async def test_join_groups_by_symbol_and_freq(
    store_with_frames: tuple[OfflineStore, _FakeDb],
) -> None:
    """Each (symbol, freq) pair should fetch independently — no cross-leakage."""
    store, _ = store_with_frames
    await store.put_many(
        [
            _frame("X", ts=10, value=1.0),
            _frame("Y", ts=15, value=2.0),
            _frame("X", ts=25, value=3.0),
        ]
    )
    joiner = PITJoiner(store, lookback_ns=1_000_000)

    out = await joiner.join_bars([_bar("X", ts=20), _bar("Y", ts=20)])

    by_symbol = {bar.symbol: feat for bar, feat in out}
    assert by_symbol["X"] is not None and by_symbol["X"].values["x"] == 1.0
    assert by_symbol["Y"] is not None and by_symbol["Y"].values["x"] == 2.0


async def test_join_preserves_input_order(
    store_with_frames: tuple[OfflineStore, _FakeDb],
) -> None:
    """Output must be aligned to the input bars list, regardless of grouping."""
    store, _ = store_with_frames
    await store.put_many([_frame("X", ts=10), _frame("Y", ts=10)])
    joiner = PITJoiner(store, lookback_ns=1_000_000)

    bars = [_bar("Y", ts=20), _bar("X", ts=20), _bar("Y", ts=30)]
    out = await joiner.join_bars(bars)

    assert [bar.symbol for bar, _ in out] == ["Y", "X", "Y"]
    assert [bar.ts_event for bar, _ in out] == [20, 20, 30]


async def test_empty_input_returns_empty_output(
    store_with_frames: tuple[OfflineStore, _FakeDb],
) -> None:
    store, _ = store_with_frames
    joiner = PITJoiner(store)
    assert await joiner.join_bars([]) == []


async def test_freq_partitioning_avoids_cross_freq_leakage(
    store_with_frames: tuple[OfflineStore, _FakeDb],
) -> None:
    """A 1d feature must not satisfy a 1m bar's PIT request."""
    store, _ = store_with_frames
    await store.put_many([_frame("X", ts=10, freq="1d", value=99.0)])
    joiner = PITJoiner(store, lookback_ns=1_000_000)

    out = await joiner.join_bars([_bar("X", ts=20, freq="1m")])
    _, feat = out[0]
    assert feat is None  # the 1d feature is not visible to a 1m query
