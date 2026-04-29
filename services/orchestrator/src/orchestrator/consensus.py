"""
orchestrator.consensus - per-symbol multi-agent prediction aggregator.

Each ``Prediction`` event carries an ``agent_id``.  The orchestrator
keeps the LATEST prediction per (agent_id, symbol) in memory, then on
each update returns a single :class:`AgentConsensus` aggregating all
non-stale agents for that symbol.

Aggregation rule (v1):

  - Drop stale predictions whose ``ts_event + horizon_ns < now_ns``,
    or whose ``ts_event + max_age_ns < now_ns`` if ``horizon_ns`` is 0.
  - direction = sum(p.direction * p.confidence) / sum(p.confidence)
                weighted by confidence
  - confidence = mean(p.confidence) across surviving agents
                 (mean, not sum, so adding agents doesn't inflate
                  confidence beyond 1.0)
  - ts_event   = max(p.ts_event)
  - horizon_ns = mean(p.horizon_ns)

A degenerate case where all surviving predictions have confidence 0
returns ``None`` (no signal).

When more agent types land (regime, sentiment, pairs), this aggregator
is the natural place to add per-source weights or non-linear combiners.
v1 keeps it linear and confidence-weighted.
"""

from __future__ import annotations

from dataclasses import dataclass

from fincept_core.schemas import Prediction


@dataclass(frozen=True)
class AgentConsensus:
    """Aggregated direction + confidence across agents for one symbol."""

    symbol: str
    direction: float
    confidence: float
    ts_event: int
    horizon_ns: int
    contributing_agents: tuple[str, ...]


@dataclass(frozen=True)
class _Cached:
    direction: float
    confidence: float
    ts_event: int
    horizon_ns: int


class ConsensusBuilder:
    """Per-symbol latest-prediction cache + aggregator.

    Construction params:
      ``max_age_ns``  Predictions older than this (relative to ``now_ns``)
                     are ignored even if their declared horizon hasn't
                     expired.  Defaults to 5 minutes - generous enough
                     for 1m-cadence agents to survive a brief consumer
                     hiccup, tight enough to drop a crashed agent.
    """

    def __init__(self, *, max_age_ns: int = 5 * 60 * 1_000_000_000) -> None:
        self._max_age_ns = max_age_ns
        # symbol -> agent_id -> _Cached
        self._latest: dict[str, dict[str, _Cached]] = {}

    def update(self, prediction: Prediction) -> None:
        """Record the latest prediction for (agent, symbol)."""
        per_symbol = self._latest.setdefault(prediction.symbol, {})
        per_symbol[prediction.agent_id] = _Cached(
            direction=prediction.direction,
            confidence=prediction.confidence,
            ts_event=prediction.ts_event,
            horizon_ns=prediction.horizon_ns,
        )

    def consensus(self, symbol: str, *, now_ns: int) -> AgentConsensus | None:
        """Aggregate across non-stale agents, or return None if empty."""
        per_symbol = self._latest.get(symbol)
        if not per_symbol:
            return None

        fresh: list[tuple[str, _Cached]] = []
        for agent_id, cached in per_symbol.items():
            if self._is_stale(cached, now_ns=now_ns):
                continue
            fresh.append((agent_id, cached))
        if not fresh:
            return None

        total_conf = sum(c.confidence for _, c in fresh)
        if total_conf <= 0:
            return None

        weighted_direction = (
            sum(c.direction * c.confidence for _, c in fresh) / total_conf
        )
        avg_confidence = total_conf / len(fresh)
        ts_event = max(c.ts_event for _, c in fresh)
        horizon_ns = sum(c.horizon_ns for _, c in fresh) // len(fresh)

        return AgentConsensus(
            symbol=symbol,
            direction=weighted_direction,
            confidence=avg_confidence,
            ts_event=ts_event,
            horizon_ns=horizon_ns,
            contributing_agents=tuple(sorted(a for a, _ in fresh)),
        )

    def _is_stale(self, cached: _Cached, *, now_ns: int) -> bool:
        # Stale if older than max_age_ns OR (if horizon set) past horizon.
        if now_ns - cached.ts_event > self._max_age_ns:
            return True
        if cached.horizon_ns > 0 and now_ns - cached.ts_event > cached.horizon_ns:
            return True
        return False
