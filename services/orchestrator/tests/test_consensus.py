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
