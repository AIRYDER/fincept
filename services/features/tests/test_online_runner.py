"""
Tests for features.online.OnlineRunner — bars-in / FeatureFrame-out.

Uses an in-memory FakeProducer (capture-list) so no Redis or consumer
loop is required.  Calls ``on_bar`` directly with hand-built BarEvents.
"""

from __future__ import annotations

from decimal import Decimal

import fakeredis.aioredis

from features.online import OnlineRunner
from features.store import OnlineStore
from fincept_bus.streams import STREAM_FEATURES_ONLINE
from fincept_core.events import Event
from fincept_core.schemas import (
    AssetClass,
    BarEvent,
    FeatureFrame,
    TradeEvent,
    Venue,
)


class FakeProducer:
    def __init__(self) -> None:
        self.published: list[tuple[str, Event]] = []

    async def publish(self, stream: str, event: Event) -> str:
        self.published.append((stream, event))
        return f"0-{len(self.published)}"

    @property
    def frames(self) -> list[FeatureFrame]:
        return [
            ev.payload
            for stream, ev in self.published
            if stream == STREAM_FEATURES_ONLINE and isinstance(ev.payload, FeatureFrame)
        ]


def _bar(symbol: str, *, ts_event: int, close: str, prev_close: str | None = None) -> BarEvent:
    """Construct a 1m bar with a benign OHLC around *close*."""
    c = Decimal(close)
    o = Decimal(prev_close) if prev_close is not None else c
    return BarEvent(
        venue=Venue.BINANCE,
        symbol=symbol,
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=ts_event,
        ts_recv=ts_event,
        freq="1m",
        open=o,
        high=c + Decimal("1"),
        low=c - Decimal("1"),
        close=c,
        volume=Decimal("10"),
        trades=5,
        vwap=c,
    )


# ---------------------------------------------------------------------------
# Single-bar smoke
# ---------------------------------------------------------------------------


async def test_single_bar_publishes_one_feature_frame() -> None:
    producer = FakeProducer()
    runner = OnlineRunner(producer, benchmark_symbol="BTC-USD")
    await runner.on_bar(_bar("BTC-USD", ts_event=1_000, close="100"))

    assert len(producer.frames) == 1
    frame = producer.frames[0]
    assert frame.symbol == "BTC-USD"
    assert frame.ts_event == 1_000
    assert frame.freq == "1m"


async def test_feature_frame_merges_price_vol_and_cross_keys() -> None:
    producer = FakeProducer()
    runner = OnlineRunner(producer, benchmark_symbol="BTC-USD")
    await runner.on_bar(_bar("BTC-USD", ts_event=1_000, close="100"))

    keys = set(producer.frames[0].values)
    assert {"ret_log_1", "ret_simple_1", "mom_5", "mom_20", "mom_60"} <= keys
    assert {"vol_rs_20", "vol_park_20", "vol_gk_20"} <= keys
    assert {"beta_BTC-USD_60", "corr_BTC-USD_60"} <= keys


async def test_first_bar_yields_all_none_values() -> None:
    """No previous close → returns None → vol windows None → cross None."""
    producer = FakeProducer()
    runner = OnlineRunner(producer, benchmark_symbol="BTC-USD")
    await runner.on_bar(_bar("BTC-USD", ts_event=1_000, close="100"))

    assert all(v is None for v in producer.frames[0].values.values())


# ---------------------------------------------------------------------------
# Cross-feature wiring
# ---------------------------------------------------------------------------


async def test_benchmark_self_compare_eventually_yields_beta_one() -> None:
    """When the only stream is the benchmark itself, beta_BTC_w → 1 once
    enough rets have accumulated."""
    producer = FakeProducer()
    runner = OnlineRunner(producer, benchmark_symbol="BTC-USD")

    # Need >= 60 rets for the corr_BTC-USD_60 window. Feed alternating prices
    # so log returns are non-zero (bench variance > 0 → beta defined).
    closes = [100.0 if i % 2 == 0 else 110.0 for i in range(70)]
    for i, c in enumerate(closes):
        await runner.on_bar(_bar("BTC-USD", ts_event=1_000 * (i + 1), close=str(c)))

    last = producer.frames[-1].values
    assert last["beta_BTC-USD_60"] is not None
    assert abs(last["beta_BTC-USD_60"] - 1.0) < 1e-9


async def test_non_benchmark_symbol_uses_existing_bench_history() -> None:
    """ETH-USD bars after BTC-USD warmup should produce beta that references
    the benchmark deque, not the symbol's own history.

    To get a clean beta = +1, we feed the same number of bars (61) to both
    so the last-60 windows of bench and sym deques are positionally aligned
    with the same alternation phase.  When BTC and ETH counts diverge, the
    deque tails fall out of phase and beta flips sign — that's correct math
    but a fragile test target.
    """
    producer = FakeProducer()
    runner = OnlineRunner(producer, benchmark_symbol="BTC-USD")

    bar_count = 61  # 60 log-rets after the first bar bootstraps.
    for i in range(bar_count):
        c = 100.0 if i % 2 == 0 else 110.0
        await runner.on_bar(_bar("BTC-USD", ts_event=1_000 * (i + 1), close=str(c)))
    for i in range(bar_count):
        c = 50.0 if i % 2 == 0 else 55.0
        await runner.on_bar(_bar("ETH-USD", ts_event=1_000_000 + 1_000 * i, close=str(c)))

    eth_frames = [f for f in producer.frames if f.symbol == "ETH-USD"]
    last = eth_frames[-1].values
    # Same alternation, same magnitudes → beta = +1.0 exactly.
    assert last["beta_BTC-USD_60"] is not None
    assert abs(last["beta_BTC-USD_60"] - 1.0) < 1e-9


async def test_two_symbols_have_independent_price_state() -> None:
    """ETH momentum should not be polluted by BTC's price history."""
    producer = FakeProducer()
    runner = OnlineRunner(producer, benchmark_symbol="BTC-USD")

    # 6 BTC bars climbing 100 → 105 (mom_5 will populate on the 6th).
    for i, px in enumerate([100, 101, 102, 103, 104, 105]):
        await runner.on_bar(_bar("BTC-USD", ts_event=1_000 * (i + 1), close=str(px)))

    # First ETH bar — mom_5 must be None (only one ETH close so far).
    await runner.on_bar(_bar("ETH-USD", ts_event=10_000, close="50"))

    eth_first = next(f for f in producer.frames if f.symbol == "ETH-USD")
    assert eth_first.values["mom_5"] is None


# ---------------------------------------------------------------------------
# handle_event dispatch
# ---------------------------------------------------------------------------


async def test_handle_event_ignores_non_bar_payloads() -> None:
    producer = FakeProducer()
    runner = OnlineRunner(producer, benchmark_symbol="BTC-USD")

    trade = TradeEvent(
        venue=Venue.BINANCE,
        symbol="BTC-USD",
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=1,
        ts_recv=2,
        seq=1,
        price=Decimal("100"),
        size=Decimal("1"),
    )
    await runner.handle_event(Event(type="trade", payload=trade))
    assert producer.frames == []


async def test_handle_event_dispatches_bar_to_on_bar() -> None:
    producer = FakeProducer()
    runner = OnlineRunner(producer, benchmark_symbol="BTC-USD")

    bar = _bar("BTC-USD", ts_event=1_000, close="100")
    await runner.handle_event(Event(type="bar", payload=bar))
    assert len(producer.frames) == 1


async def test_on_bar_caches_latest_frame_when_store_is_wired() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    try:
        store = OnlineStore(redis)
        producer = FakeProducer()
        runner = OnlineRunner(
            producer,
            benchmark_symbol="BTC-USD",
            online_store=store,
        )

        await runner.on_bar(_bar("BTC-USD", ts_event=1_000, close="100"))

        cached = await store.get_latest("BTC-USD", freq="1m")
        assert cached is not None
        assert cached.symbol == "BTC-USD"
        assert cached.ts_event == 1_000
    finally:
        await redis.aclose()


# ---------------------------------------------------------------------------
# Default benchmark
# ---------------------------------------------------------------------------


def test_default_benchmark_falls_back_to_btc_usd_when_universe_empty(
    monkeypatch: object,
) -> None:
    """OnlineRunner with no explicit benchmark uses Settings.UNIVERSE[0],
    falling back to BTC-USD if the universe is empty."""
    from fincept_core.config import Settings

    Settings.clear_cache()
    runner = OnlineRunner(FakeProducer())
    # The default universe in Settings is ["BTC-USD","ETH-USD","SOL-USD"].
    assert runner.benchmark == "BTC-USD"
