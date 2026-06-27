"""Tests for orchestrator.consensus.ConsensusBuilder."""

from __future__ import annotations

from fincept_core.schemas import Prediction
from orchestrator.consensus import ConsensusBuilder


def _pred(
    *,
    agent_id: str = "gbm.v1",
    symbol: str = "BTC-USD",
    direction: float = 0.5,
    confidence: float = 0.8,
    ts_event: int = 1_000_000_000,
    horizon_ns: int = 60 * 1_000_000_000,
) -> Prediction:
    return Prediction(
        agent_id=agent_id,
        symbol=symbol,
        ts_event=ts_event,
        horizon_ns=horizon_ns,
        direction=direction,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Empty / single agent
# ---------------------------------------------------------------------------


def test_empty_returns_none() -> None:
    cb = ConsensusBuilder()
    assert cb.consensus("BTC-USD", now_ns=2_000_000_000) is None


def test_single_agent_round_trips() -> None:
    cb = ConsensusBuilder()
    cb.update(_pred(direction=0.7, confidence=0.9))
    out = cb.consensus("BTC-USD", now_ns=1_500_000_000)
    assert out is not None
    assert out.symbol == "BTC-USD"
    assert out.direction == 0.7
    assert out.confidence == 0.9
    assert out.contributing_agents == ("gbm.v1",)


# ---------------------------------------------------------------------------
# Multi-agent aggregation
# ---------------------------------------------------------------------------


def test_two_agents_confidence_weighted_direction() -> None:
    cb = ConsensusBuilder()
    cb.update(_pred(agent_id="a", direction=1.0, confidence=0.8))
    cb.update(_pred(agent_id="b", direction=-0.5, confidence=0.4))
    out = cb.consensus("BTC-USD", now_ns=1_500_000_000)
    assert out is not None
    # Weighted: (1.0*0.8 + (-0.5)*0.4) / (0.8 + 0.4) = (0.8 - 0.2)/1.2 = 0.5
    assert abs(out.direction - 0.5) < 1e-9
    # Confidence is mean: (0.8 + 0.4) / 2 = 0.6
    assert abs(out.confidence - 0.6) < 1e-9
    assert out.contributing_agents == ("a", "b")


def test_three_agents_in_agreement_strong_direction() -> None:
    cb = ConsensusBuilder()
    cb.update(_pred(agent_id="a", direction=0.8, confidence=0.9))
    cb.update(_pred(agent_id="b", direction=0.7, confidence=0.6))
    cb.update(_pred(agent_id="c", direction=0.9, confidence=0.7))
    out = cb.consensus("BTC-USD", now_ns=1_500_000_000)
    assert out is not None
    # All bullish; weighted average should be positive and roughly 0.8.
    assert 0.7 < out.direction < 0.9


def test_zero_total_confidence_returns_none() -> None:
    cb = ConsensusBuilder()
    cb.update(_pred(agent_id="a", direction=0.5, confidence=0.0))
    cb.update(_pred(agent_id="b", direction=-0.5, confidence=0.0))
    out = cb.consensus("BTC-USD", now_ns=1_500_000_000)
    assert out is None


# ---------------------------------------------------------------------------
# Update overrides
# ---------------------------------------------------------------------------


def test_same_agent_update_replaces_previous() -> None:
    cb = ConsensusBuilder()
    cb.update(_pred(agent_id="a", direction=0.5))
    cb.update(_pred(agent_id="a", direction=-0.3))
    out = cb.consensus("BTC-USD", now_ns=1_500_000_000)
    assert out is not None
    assert out.direction == -0.3


# ---------------------------------------------------------------------------
# Per-symbol isolation
# ---------------------------------------------------------------------------


def test_different_symbols_dont_interfere() -> None:
    cb = ConsensusBuilder()
    cb.update(_pred(symbol="BTC-USD", direction=0.5))
    cb.update(_pred(symbol="ETH-USD", direction=-0.5))
    btc = cb.consensus("BTC-USD", now_ns=1_500_000_000)
    eth = cb.consensus("ETH-USD", now_ns=1_500_000_000)
    assert btc is not None
    assert eth is not None
    assert btc.direction == 0.5
    assert eth.direction == -0.5


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_max_age_drops_old_predictions() -> None:
    cb = ConsensusBuilder(max_age_ns=10_000_000_000)  # 10s window
    cb.update(_pred(direction=0.5, ts_event=0, horizon_ns=0))
    # 100s later -> stale
    out = cb.consensus("BTC-USD", now_ns=100_000_000_000)
    assert out is None


def test_positive_horizon_can_outlive_default_max_age() -> None:
    cb = ConsensusBuilder(max_age_ns=10_000_000_000)  # 10s fallback
    cb.update(_pred(direction=0.5, ts_event=0, horizon_ns=30_000_000_000))
    out = cb.consensus("BTC-USD", now_ns=20_000_000_000)
    assert out is not None
    assert out.direction == 0.5


def test_horizon_drops_predictions_past_horizon() -> None:
    cb = ConsensusBuilder(max_age_ns=10**18)  # don't trip max_age
    cb.update(_pred(direction=0.5, ts_event=0, horizon_ns=5_000_000_000))
    # 6s later, past horizon -> stale
    out = cb.consensus("BTC-USD", now_ns=6_000_000_000)
    assert out is None


def test_some_agents_stale_others_fresh_returns_only_fresh() -> None:
    cb = ConsensusBuilder(max_age_ns=10_000_000_000)
    cb.update(_pred(agent_id="old", direction=1.0, ts_event=0, horizon_ns=0))
    cb.update(_pred(agent_id="new", direction=-1.0, ts_event=50_000_000_000))
    out = cb.consensus("BTC-USD", now_ns=55_000_000_000)
    assert out is not None
    assert out.contributing_agents == ("new",)
    assert out.direction == -1.0


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------


def test_evict_stale_removes_stale_entries() -> None:
    """evict_stale() should remove stale predictions from the cache."""
    cb = ConsensusBuilder(max_age_ns=10_000_000_000)  # 10s
    cb.update(_pred(agent_id="old", ts_event=0, horizon_ns=0))
    cb.update(_pred(agent_id="new", ts_event=50_000_000_000, horizon_ns=0))

    # Before eviction: 2 entries cached.
    assert cb.cached_entries == 2

    evicted = cb.evict_stale(now_ns=55_000_000_000)
    assert evicted == 1  # Only "old" is stale.
    assert cb.cached_entries == 1
    assert cb.total_evicted == 1


def test_evict_stale_removes_empty_symbols() -> None:
    """When all agents for a symbol are stale, the symbol key is removed."""
    cb = ConsensusBuilder(max_age_ns=10_000_000_000)
    cb.update(_pred(agent_id="a1", symbol="BTC-USD", ts_event=0, horizon_ns=0))
    cb.update(
        _pred(agent_id="a2", symbol="ETH-USD", ts_event=50_000_000_000, horizon_ns=0)
    )

    # At now=55s, BTC-USD's only agent is stale.
    evicted = cb.evict_stale(now_ns=55_000_000_000)
    assert evicted == 1
    assert cb.cached_symbols == 1  # Only ETH-USD remains.
    assert "BTC-USD" not in cb._latest


def test_evict_stale_with_no_entries_returns_zero() -> None:
    cb = ConsensusBuilder()
    assert cb.evict_stale(now_ns=1_000_000_000) == 0
    assert cb.total_evicted == 0


def test_evict_stale_preserves_fresh_entries() -> None:
    """Fresh entries should not be evicted."""
    cb = ConsensusBuilder(max_age_ns=10_000_000_000)
    cb.update(_pred(agent_id="fresh", ts_event=50_000_000_000, horizon_ns=0))
    evicted = cb.evict_stale(now_ns=55_000_000_000)
    assert evicted == 0
    assert cb.cached_entries == 1


def test_evict_stale_with_horizon_based_staleness() -> None:
    """Predictions with explicit horizons should be evicted when expired."""
    cb = ConsensusBuilder()
    cb.update(_pred(agent_id="h", ts_event=0, horizon_ns=5_000_000_000))  # 5s horizon
    # At 6s, past horizon -> stale.
    evicted = cb.evict_stale(now_ns=6_000_000_000)
    assert evicted == 1
    assert cb.cached_entries == 0


def test_evict_stale_accumulates_total_count() -> None:
    """total_evicted should accumulate across multiple evict_stale calls."""
    cb = ConsensusBuilder(max_age_ns=10_000_000_000)
    cb.update(_pred(agent_id="a1", symbol="BTC", ts_event=0, horizon_ns=0))
    cb.update(_pred(agent_id="a2", symbol="ETH", ts_event=0, horizon_ns=0))

    cb.evict_stale(now_ns=55_000_000_000)
    assert cb.total_evicted == 2

    cb.update(_pred(agent_id="a3", symbol="BTC", ts_event=0, horizon_ns=0))
    cb.evict_stale(now_ns=55_000_000_000)
    assert cb.total_evicted == 3


def test_cached_properties() -> None:
    """cached_symbols and cached_entries should report accurate counts."""
    cb = ConsensusBuilder()
    assert cb.cached_symbols == 0
    assert cb.cached_entries == 0

    cb.update(_pred(agent_id="a1", symbol="BTC-USD"))
    cb.update(_pred(agent_id="a2", symbol="BTC-USD"))
    cb.update(_pred(agent_id="a1", symbol="ETH-USD"))

    assert cb.cached_symbols == 2
    assert cb.cached_entries == 3


def test_eviction_does_not_affect_consensus_correctness() -> None:
    """consensus() should return the same result before and after eviction
    for fresh entries (eviction only removes stale entries, which
    consensus() already filters)."""
    cb = ConsensusBuilder(max_age_ns=10_000_000_000)
    cb.update(_pred(agent_id="old", direction=1.0, ts_event=0, horizon_ns=0))
    cb.update(
        _pred(agent_id="new", direction=-1.0, ts_event=50_000_000_000, horizon_ns=0)
    )

    before = cb.consensus("BTC-USD", now_ns=55_000_000_000)
    cb.evict_stale(now_ns=55_000_000_000)
    after = cb.consensus("BTC-USD", now_ns=55_000_000_000)

    assert before is not None
    assert after is not None
    assert before.direction == after.direction
    assert before.contributing_agents == after.contributing_agents
