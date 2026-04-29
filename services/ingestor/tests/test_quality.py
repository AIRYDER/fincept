"""
Tests for ingestor.quality.QualityMonitor — async event-driven monitor.

These tests inject a fake producer (in-memory list capture) and a fake
clock so they're fully deterministic and require no live Redis.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fincept_bus.streams import STREAM_ALERTS
from fincept_core.events import Event
from fincept_core.schemas import (
    AlertEvent,
    AssetClass,
    BookLevel,
    BookSnapshotEvent,
    Side,
    TradeEvent,
    Venue,
)
from ingestor.quality import (
    CLOCK_SKEW_BUDGET_NS,
    DEDUP_WINDOW_NS,
    STALENESS_BUDGET_NS,
    QualityMonitor,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeProducer:
    """Captures every publish call so tests can assert on them in-process."""

    def __init__(self) -> None:
        self.published: list[tuple[str, Event]] = []

    async def publish(self, stream: str, event: Event) -> str:
        self.published.append((stream, event))
        return f"0-{len(self.published)}"

    @property
    def alerts(self) -> list[AlertEvent]:
        return [
            ev.payload
            for stream, ev in self.published
            if stream == STREAM_ALERTS and isinstance(ev.payload, AlertEvent)
        ]


class FakeClock:
    """Monotonic injected clock — tests advance it explicitly."""

    def __init__(self, start_ns: int = 1_700_000_000_000_000_000) -> None:
        self.now_ns = start_ns

    def __call__(self) -> int:
        return self.now_ns

    def advance(self, delta_ns: int) -> None:
        self.now_ns += delta_ns


def _trade(
    venue: Venue,
    symbol: str,
    *,
    seq: int | None,
    ts_event: int,
    ts_recv: int,
    price: str = "100",
) -> TradeEvent:
    return TradeEvent(
        venue=venue,
        symbol=symbol,
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=ts_event,
        ts_recv=ts_recv,
        seq=seq,
        price=Decimal(price),
        size=Decimal("1"),
        side=Side.BUY,
    )


def _snapshot(
    venue: Venue,
    symbol: str,
    *,
    bid: str,
    ask: str,
    ts_event: int = 1,
    ts_recv: int = 2,
) -> BookSnapshotEvent:
    return BookSnapshotEvent(
        venue=venue,
        symbol=symbol,
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=ts_event,
        ts_recv=ts_recv,
        bids=[BookLevel(price=Decimal(bid), size=Decimal("1"))],
        asks=[BookLevel(price=Decimal(ask), size=Decimal("1"))],
    )


def _make_monitor(
    *, clock: FakeClock | None = None, dedup_window_ns: int = DEDUP_WINDOW_NS
) -> tuple[QualityMonitor, FakeProducer, FakeClock]:
    producer = FakeProducer()
    clk = clock if clock is not None else FakeClock()
    counter = [0]

    def id_factory() -> str:
        counter[0] += 1
        return f"alert-{counter[0]:08d}"

    monitor = QualityMonitor(
        producer,
        clock=clk,
        id_factory=id_factory,
        dedup_window_ns=dedup_window_ns,
    )
    return monitor, producer, clk


# ---------------------------------------------------------------------------
# Sequence gap
# ---------------------------------------------------------------------------


async def test_monotonic_seq_emits_no_alert() -> None:
    monitor, producer, _ = _make_monitor()
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=1, ts_event=1, ts_recv=2))
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=2, ts_event=3, ts_recv=4))
    assert producer.alerts == []


async def test_seq_gap_emits_warning_with_prev_current_tags() -> None:
    monitor, producer, _ = _make_monitor()
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=1, ts_event=1, ts_recv=2))
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=10, ts_event=3, ts_recv=4))

    gap_alerts = [a for a in producer.alerts if a.code == "seq_gap"]
    assert len(gap_alerts) == 1
    alert = gap_alerts[0]
    assert alert.severity == "warning"
    assert alert.source == "ingestor.quality"
    assert alert.tags["venue"] == "binance"
    assert alert.tags["symbol"] == "BTC-USDT"
    assert alert.tags["prev"] == "1"
    assert alert.tags["current"] == "10"


async def test_seq_none_never_emits_seq_gap() -> None:
    """Coinbase L2 deltas (and trades without trade_id) carry seq=None."""
    monitor, producer, _ = _make_monitor()
    await monitor.on_trade(_trade(Venue.COINBASE, "BTC-USD", seq=None, ts_event=1, ts_recv=2))
    await monitor.on_trade(_trade(Venue.COINBASE, "BTC-USD", seq=None, ts_event=3, ts_recv=4))
    assert [a for a in producer.alerts if a.code == "seq_gap"] == []


# ---------------------------------------------------------------------------
# Clock skew
# ---------------------------------------------------------------------------


async def test_clock_skew_above_budget_emits_warning() -> None:
    monitor, producer, _ = _make_monitor()
    skew = CLOCK_SKEW_BUDGET_NS + 1_000_000  # 1ms over
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=1, ts_event=0, ts_recv=skew))
    skew_alerts = [a for a in producer.alerts if a.code == "clock_skew"]
    assert len(skew_alerts) == 1
    assert skew_alerts[0].tags["skew_ns"] == str(skew)


async def test_clock_skew_within_budget_emits_nothing() -> None:
    monitor, producer, _ = _make_monitor()
    await monitor.on_trade(
        _trade(Venue.BINANCE, "BTC-USDT", seq=1, ts_event=0, ts_recv=CLOCK_SKEW_BUDGET_NS)
    )
    assert [a for a in producer.alerts if a.code == "clock_skew"] == []


async def test_negative_skew_does_not_alert() -> None:
    """ts_recv < ts_event (host clock behind venue) shouldn't false-fire."""
    monitor, producer, _ = _make_monitor()
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=1, ts_event=1000, ts_recv=500))
    assert producer.alerts == []


# ---------------------------------------------------------------------------
# Cross-venue spread
# ---------------------------------------------------------------------------


async def test_single_venue_snapshot_no_cross_alert() -> None:
    monitor, producer, _ = _make_monitor()
    await monitor.on_book(_snapshot(Venue.COINBASE, "BTC-USD", bid="100000", ask="100100"))
    assert producer.alerts == []


async def test_cross_spread_within_threshold_no_alert() -> None:
    """50 bps default threshold; 10 bps divergence should not fire."""
    monitor, producer, _ = _make_monitor()
    # mid 100050 vs mid 100150 → 100 / 100050 → ~10 bps
    await monitor.on_book(_snapshot(Venue.COINBASE, "BTC-USD", bid="100000", ask="100100"))
    await monitor.on_book(_snapshot(Venue.KRAKEN, "BTC-USD", bid="100100", ask="100200"))
    assert [a for a in producer.alerts if a.code == "cross_spread"] == []


async def test_cross_spread_above_threshold_emits_critical() -> None:
    """50 bps default; 100 vs 99.55 mids → ~45 bps not enough; bump to ~500 bps."""
    monitor, producer, _ = _make_monitor()
    # mid 100050 vs mid 95050 → diff 5000 / 95050 → ~526 bps
    await monitor.on_book(_snapshot(Venue.COINBASE, "BTC-USD", bid="100000", ask="100100"))
    await monitor.on_book(_snapshot(Venue.KRAKEN, "BTC-USD", bid="95000", ask="95100"))

    cross = [a for a in producer.alerts if a.code == "cross_spread"]
    assert len(cross) == 1
    alert = cross[0]
    assert alert.severity == "critical"
    assert alert.tags["symbol"] == "BTC-USD"
    assert "mid_coinbase" in alert.tags
    assert "mid_kraken" in alert.tags
    assert Decimal(alert.tags["spread_bps"]) > Decimal(50)


async def test_cross_spread_only_compares_canonical_symbol() -> None:
    """BTC-USDT (Binance) and BTC-USD (Coinbase) must NOT cross-compare."""
    monitor, producer, _ = _make_monitor()
    await monitor.on_book(_snapshot(Venue.BINANCE, "BTC-USDT", bid="100000", ask="100100"))
    await monitor.on_book(_snapshot(Venue.COINBASE, "BTC-USD", bid="50000", ask="50100"))
    # Same canonical "BTC-USD" string never appears on both sides → no compare.
    assert [a for a in producer.alerts if a.code == "cross_spread"] == []


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


async def test_staleness_above_budget_emits_warning() -> None:
    clk = FakeClock()
    monitor, producer, _ = _make_monitor(clock=clk)
    await monitor.on_trade(
        _trade(Venue.BINANCE, "BTC-USDT", seq=1, ts_event=clk.now_ns, ts_recv=clk.now_ns)
    )
    clk.advance(STALENESS_BUDGET_NS + 1_000_000_000)  # 1s past budget
    await monitor.staleness_check()

    stale = [a for a in producer.alerts if a.code == "stale"]
    assert len(stale) == 1
    assert stale[0].tags["venue"] == "binance"
    assert stale[0].tags["symbol"] == "BTC-USDT"


async def test_staleness_within_budget_no_alert() -> None:
    clk = FakeClock()
    monitor, producer, _ = _make_monitor(clock=clk)
    await monitor.on_trade(
        _trade(Venue.BINANCE, "BTC-USDT", seq=1, ts_event=clk.now_ns, ts_recv=clk.now_ns)
    )
    clk.advance(STALENESS_BUDGET_NS - 1_000_000_000)  # 1s under budget
    await monitor.staleness_check()
    assert [a for a in producer.alerts if a.code == "stale"] == []


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


async def test_dedup_same_code_and_tags_within_window_emits_once() -> None:
    clk = FakeClock()
    monitor, producer, _ = _make_monitor(clock=clk)
    # First gap: prev=1, current=10 — fires.
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=1, ts_event=1, ts_recv=2))
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=10, ts_event=3, ts_recv=4))
    clk.advance(1_000_000_000)  # 1s later, still inside the 30 s dedup window.
    # Force the same gap signature again by rewinding state — alert key is
    # (code, frozenset(tags)) so this is the same key as the first emission.
    monitor._last_seq[("binance", "BTC-USDT")] = 1  # type: ignore[attr-defined]
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=10, ts_event=5, ts_recv=6))

    gap_alerts = [a for a in producer.alerts if a.code == "seq_gap"]
    assert len(gap_alerts) == 1


async def test_dedup_different_tags_both_fire() -> None:
    monitor, producer, _ = _make_monitor()
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=1, ts_event=1, ts_recv=2))
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=10, ts_event=3, ts_recv=4))
    await monitor.on_trade(_trade(Venue.COINBASE, "ETH-USD", seq=1, ts_event=5, ts_recv=6))
    await monitor.on_trade(_trade(Venue.COINBASE, "ETH-USD", seq=10, ts_event=7, ts_recv=8))

    gap_alerts = [a for a in producer.alerts if a.code == "seq_gap"]
    assert len(gap_alerts) == 2  # different (venue, symbol) → distinct keys


async def test_dedup_re_fires_after_window_expires() -> None:
    clk = FakeClock()
    monitor, producer, _ = _make_monitor(clock=clk, dedup_window_ns=1_000_000_000)
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=1, ts_event=1, ts_recv=2))
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=10, ts_event=3, ts_recv=4))
    # Advance past the window, force same-tagged gap again:
    clk.advance(2_000_000_000)
    monitor._last_seq[("binance", "BTC-USDT")] = 1  # type: ignore[attr-defined]
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=10, ts_event=5, ts_recv=6))

    gap_alerts = [a for a in producer.alerts if a.code == "seq_gap"]
    assert len(gap_alerts) == 2


# ---------------------------------------------------------------------------
# Alert envelope
# ---------------------------------------------------------------------------


async def test_alert_event_publishes_to_alerts_stream() -> None:
    monitor, producer, _ = _make_monitor()
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=1, ts_event=1, ts_recv=2))
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=10, ts_event=3, ts_recv=4))

    streams = {stream for stream, _ in producer.published}
    assert streams == {STREAM_ALERTS}


async def test_alert_event_has_required_fields() -> None:
    monitor, producer, clk = _make_monitor()
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=1, ts_event=1, ts_recv=2))
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=10, ts_event=3, ts_recv=4))

    alert = producer.alerts[0]
    assert alert.alert_id.startswith("alert-")
    assert alert.ts_event == clk.now_ns
    assert alert.severity in {"info", "warning", "critical"}
    assert alert.source == "ingestor.quality"
    assert alert.code == "seq_gap"


def test_quality_monitor_can_be_imported_via_package() -> None:
    """Import-via-package smoke test."""
    from ingestor import QualityMonitor as Monitor

    assert Monitor is QualityMonitor


@pytest.mark.parametrize(
    ("delta", "should_alert"),
    [(CLOCK_SKEW_BUDGET_NS - 1, False), (CLOCK_SKEW_BUDGET_NS + 1, True)],
)
async def test_clock_skew_threshold_is_strict(delta: int, should_alert: bool) -> None:
    monitor, producer, _ = _make_monitor()
    await monitor.on_trade(_trade(Venue.BINANCE, "BTC-USDT", seq=1, ts_event=0, ts_recv=delta))
    fired = bool([a for a in producer.alerts if a.code == "clock_skew"])
    assert fired is should_alert
