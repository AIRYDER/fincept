"""
Tests for features.offline.backfill.

Inject fake bar reader + fake offline store so the backfill orchestration
runs deterministically with no DB.  The bit-identical guarantee against
the online runner is structural — both call ``FeatureComputer`` — and
pinned by ``test_computer.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

import pytest

from features.offline import backfill
from features.store import OfflineStore
from fincept_core.schemas import AssetClass, BarEvent, FeatureFrame, Venue


def _bar(symbol: str, ts: int, *, close: str = "100") -> BarEvent:
    c = Decimal(close)
    return BarEvent(
        venue=Venue.BINANCE,
        symbol=symbol,
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=ts,
        ts_recv=ts,
        freq="1m",
        open=c,
        high=c + Decimal("1"),
        low=c - Decimal("1"),
        close=c,
        volume=Decimal("10"),
        trades=5,
    )


class _FakeBarReader:
    def __init__(self, bars_by_symbol: dict[str, list[BarEvent]]) -> None:
        self._bars = bars_by_symbol
        self.calls: list[tuple[str, str, int, int]] = []

    async def __call__(self, symbol: str, freq: str, start_ns: int, end_ns: int) -> list[BarEvent]:
        self.calls.append((symbol, freq, start_ns, end_ns))
        return [b for b in self._bars.get(symbol, []) if start_ns <= b.ts_event < end_ns]


class _CapturingStore:
    def __init__(self) -> None:
        self.put_calls: list[list[FeatureFrame]] = []
        self._all: dict[tuple[str, str, int], FeatureFrame] = {}

    async def write(self, frames: Iterable[FeatureFrame]) -> int:
        batch = list(frames)
        self.put_calls.append(batch)
        for f in batch:
            self._all[(f.symbol, f.freq, f.ts_event)] = f
        return len(batch)

    async def read(self, symbol: str, freq: str, start_ns: int, end_ns: int) -> list[FeatureFrame]:
        return sorted(
            (
                f
                for (s, fr, ts), f in self._all.items()
                if s == symbol and fr == freq and start_ns <= ts < end_ns
            ),
            key=lambda f: f.ts_event,
        )


def _make_store() -> tuple[OfflineStore, _CapturingStore]:
    capturing = _CapturingStore()
    store = OfflineStore(write_fn=capturing.write, read_fn=capturing.read)
    return store, capturing


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_backfill_writes_features_for_each_bar() -> None:
    bars = {
        "BTC-USD": [_bar("BTC-USD", ts=1_000 * i) for i in range(1, 4)],
    }
    reader = _FakeBarReader(bars)
    store, capturing = _make_store()

    n = await backfill(
        ["BTC-USD"], "1m", 0, 10_000, store=store, bar_reader=reader, benchmark="BTC-USD"
    )
    assert n == 3
    assert sum(len(batch) for batch in capturing.put_calls) == 3


async def test_backfill_processes_benchmark_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ETH-USD must be passed to read_bars only AFTER BTC-USD so the
    benchmark deque is populated when ETH cross features are computed."""
    bars = {
        "BTC-USD": [_bar("BTC-USD", ts=1_000 * i) for i in range(1, 4)],
        "ETH-USD": [_bar("ETH-USD", ts=1_000 * i) for i in range(1, 4)],
    }
    reader = _FakeBarReader(bars)
    store, _ = _make_store()

    # Caller passes ETH first; backfill must reorder.
    await backfill(
        ["ETH-USD", "BTC-USD"],
        "1m",
        0,
        10_000,
        store=store,
        bar_reader=reader,
        benchmark="BTC-USD",
    )
    # First call to the reader should be for BTC-USD (the benchmark).
    assert reader.calls[0][0] == "BTC-USD"
    assert reader.calls[1][0] == "ETH-USD"


async def test_backfill_is_idempotent_on_re_run() -> None:
    """Running backfill twice over the same range must produce identical
    feature values (ON CONFLICT DO UPDATE replaces with the same data)."""
    bars = {
        "BTC-USD": [
            _bar("BTC-USD", ts=1_000, close="100"),
            _bar("BTC-USD", ts=2_000, close="105"),
            _bar("BTC-USD", ts=3_000, close="103"),
        ]
    }
    reader = _FakeBarReader(bars)
    store, capturing = _make_store()

    await backfill(
        ["BTC-USD"], "1m", 0, 10_000, store=store, bar_reader=reader, benchmark="BTC-USD"
    )
    first = sorted(capturing._all.values(), key=lambda f: f.ts_event)
    first_values = [f.values for f in first]

    await backfill(
        ["BTC-USD"], "1m", 0, 10_000, store=store, bar_reader=reader, benchmark="BTC-USD"
    )
    second = sorted(capturing._all.values(), key=lambda f: f.ts_event)
    second_values = [f.values for f in second]

    assert first_values == second_values


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_backfill_with_no_symbols_is_noop() -> None:
    store, capturing = _make_store()
    n = await backfill([], "1m", 0, 10_000, store=store, bar_reader=_FakeBarReader({}))
    assert n == 0
    assert capturing.put_calls == []


async def test_backfill_rejects_inverted_range() -> None:
    store, _ = _make_store()
    with pytest.raises(ValueError, match="must be < end_ns"):
        await backfill(
            ["BTC-USD"],
            "1m",
            10_000,
            10_000,
            store=store,
            bar_reader=_FakeBarReader({}),
        )


async def test_backfill_skips_symbol_with_no_bars() -> None:
    """Empty universe member is logged but doesn't break the run."""
    bars = {
        "BTC-USD": [_bar("BTC-USD", ts=1_000)],
        "MISSING": [],
    }
    reader = _FakeBarReader(bars)
    store, _capturing = _make_store()

    n = await backfill(
        ["BTC-USD", "MISSING"],
        "1m",
        0,
        10_000,
        store=store,
        bar_reader=reader,
        benchmark="BTC-USD",
    )
    assert n == 1


async def test_backfill_passes_through_canonical_inputs_to_reader() -> None:
    bars = {"BTC-USD": [_bar("BTC-USD", ts=1_000)]}
    reader = _FakeBarReader(bars)
    store, _capturing = _make_store()
    await backfill(
        ["BTC-USD"],
        "1m",
        100,
        9_999,
        store=store,
        bar_reader=reader,
        benchmark="BTC-USD",
    )
    assert reader.calls == [("BTC-USD", "1m", 100, 9_999)]
