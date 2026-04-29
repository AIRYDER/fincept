"""Tests for ingestor.quality — gap + latency tracking."""

from __future__ import annotations

import pytest

from ingestor.quality import QualityMonitor


def test_observe_no_gap_for_consecutive_seqs() -> None:
    q = QualityMonitor()
    q.observe("binance", "BTC-USDT", seq=1, ts_event=100, ts_recv=200)
    q.observe("binance", "BTC-USDT", seq=2, ts_event=300, ts_recv=400)
    q.observe("binance", "BTC-USDT", seq=3, ts_event=500, ts_recv=600)

    snap = q.snapshot()
    assert len(snap) == 1
    assert snap[0].total_gaps == 0
    assert snap[0].last_seq == 3


def test_observe_detects_gap_when_seq_skips_forward() -> None:
    q = QualityMonitor()
    q.observe("binance", "BTC-USDT", seq=1, ts_event=0, ts_recv=0)
    q.observe("binance", "BTC-USDT", seq=5, ts_event=0, ts_recv=0)
    # Missing 2, 3, 4 → gap=3.
    snap = q.snapshot()
    assert snap[0].total_gaps == 3
    assert snap[0].last_seq == 5


def test_observe_does_not_advance_last_seq_on_regression() -> None:
    """Out-of-order delivery shouldn't pretend a gap occurred on the next msg."""
    q = QualityMonitor()
    q.observe("binance", "BTC-USDT", seq=10, ts_event=0, ts_recv=0)
    q.observe("binance", "BTC-USDT", seq=5, ts_event=0, ts_recv=0)  # OOO
    q.observe("binance", "BTC-USDT", seq=11, ts_event=0, ts_recv=0)

    snap = q.snapshot()
    assert snap[0].last_seq == 11
    # Only the original 10 → 11 transition is consecutive; no gap.
    assert snap[0].total_gaps == 0


def test_observe_tracks_max_latency() -> None:
    q = QualityMonitor()
    q.observe("binance", "BTC-USDT", seq=None, ts_event=100, ts_recv=200)  # 100 ns
    q.observe("binance", "BTC-USDT", seq=None, ts_event=100, ts_recv=500)  # 400 ns
    q.observe("binance", "BTC-USDT", seq=None, ts_event=100, ts_recv=300)  # 200 ns
    snap = q.snapshot()
    assert snap[0].max_latency_ns == 400


def test_observe_clamps_negative_latency_to_zero() -> None:
    """Clock skew → ts_recv < ts_event must not corrupt metrics."""
    q = QualityMonitor()
    q.observe("binance", "BTC-USDT", seq=None, ts_event=1000, ts_recv=500)
    snap = q.snapshot()
    assert snap[0].max_latency_ns == 0
    assert snap[0].p99_latency_ns == 0


def test_p99_latency_is_finite() -> None:
    q = QualityMonitor(latency_window=128)
    for i in range(120):
        q.observe("binance", "BTC-USDT", seq=None, ts_event=0, ts_recv=i)
    snap = q.snapshot()
    # 120 samples 0..119; p99 ≈ 119 (idx int(round(0.99 * 119)) = 118).
    assert snap[0].p99_latency_ns >= 100
    assert snap[0].samples == 120


def test_separate_keys_per_venue_symbol() -> None:
    q = QualityMonitor()
    q.observe("binance", "BTC-USDT", seq=1, ts_event=0, ts_recv=0)
    q.observe("coinbase", "BTC-USD", seq=1, ts_event=0, ts_recv=0)
    snap = q.snapshot()
    keys = {s.key for s in snap}
    assert keys == {"binance:BTC-USDT", "coinbase:BTC-USD"}


def test_latency_window_must_be_at_least_16() -> None:
    with pytest.raises(ValueError, match="latency_window"):
        QualityMonitor(latency_window=4)


def test_snapshot_with_no_observations_is_empty() -> None:
    assert QualityMonitor().snapshot() == []
