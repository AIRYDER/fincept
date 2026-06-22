"""
quant_foundry.leaderboard_expanded — expanded tournament leaderboards (TASK-0701).

Expands the basic leaderboard (TASK-0404) with:
- **Horizon slices:** score by horizon (1h, 4h, 1d, etc.).
- **Regime slices:** score by market regime (trending, ranging, volatile).
- **Symbol-cluster slices:** score by symbol cluster (tech, energy, etc.).
- **Baseline deltas:** how much a model beats the baseline.
- **Calibration summaries:** Brier score + reliability.
- **Decay indicators:** stale/decayed flags + days since last settlement.

Key invariants:
- **A model can rank high in one horizon and low in another.** The
  ``ranked_by_horizon()`` method produces per-horizon rankings.
- **Stale or decayed models are flagged.** Stale/decayed models are pushed
  to the bottom of the overall ranking, regardless of score.
- **Leaderboard explains why a model ranks where it does.** The
  ``explain()`` method returns a ``LeaderboardExplanation`` with the rank,
  score, baseline delta, decay indicator, and horizon/regime/cluster scores.

File-disjoint from my `leaderboard.py` + `tournament.py` (read-only imports).
Does NOT modify them (avoids breaking existing TASK-0404 tests).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Slices
# ---------------------------------------------------------------------------


class HorizonSlice(BaseModel):
    """A model's score for a specific horizon (e.g. 1h, 4h, 1d)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    horizon: str
    score: float


class RegimeSlice(BaseModel):
    """A model's score for a specific market regime (e.g. trending, ranging)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    regime: str
    score: float


class SymbolClusterSlice(BaseModel):
    """A model's score for a specific symbol cluster (e.g. tech, energy)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cluster: str
    score: float


# ---------------------------------------------------------------------------
# Baseline delta
# ---------------------------------------------------------------------------


class BaselineDelta(BaseModel):
    """How much a model beats (or loses to) the baseline model.

    A positive delta means the model beats the baseline. A negative delta
    means the model underperforms the baseline.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    baseline_model_id: str
    delta: float
    baseline_score: float


# ---------------------------------------------------------------------------
# Calibration summary
# ---------------------------------------------------------------------------


class CalibrationSummary(BaseModel):
    """Confidence calibration summary for a model.

    Carries the Brier score (lower is better), reliability (higher is
    better), and the number of reliability bins.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    brier_score: float
    reliability: float
    n_bins: int = 10


# ---------------------------------------------------------------------------
# Decay indicator
# ---------------------------------------------------------------------------


class DecayIndicator(BaseModel):
    """Decay indicator for a model.

    Flags whether a model is stale (hasn't settled in a while) or decayed
    (performance has degraded). Carries the decay score (0 = no decay,
    1 = fully decayed) and days since the last settlement.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decay_score: float = 0.0
    is_stale: bool = False
    is_decayed: bool = False
    days_since_last_settlement: int = 0


# ---------------------------------------------------------------------------
# Expanded leaderboard entry
# ---------------------------------------------------------------------------


class ExpandedLeaderboardEntry(BaseModel):
    """An expanded leaderboard entry with slices + deltas + calibration + decay.

    Frozen + extra='forbid'. Carries the model_id, total_score, horizon/
    regime/symbol-cluster slices, baseline delta, calibration summary, and
    decay indicator.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    total_score: float
    settled_count: int = 0
    horizon_slices: list[HorizonSlice] = []
    regime_slices: list[RegimeSlice] = []
    symbol_cluster_slices: list[SymbolClusterSlice] = []
    baseline_delta: BaselineDelta | None = None
    calibration_summary: CalibrationSummary | None = None
    decay_indicator: DecayIndicator | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for audit/persistence."""
        return {
            "model_id": self.model_id,
            "total_score": self.total_score,
            "settled_count": self.settled_count,
            "horizon_slices": [s.model_dump() for s in self.horizon_slices],
            "regime_slices": [s.model_dump() for s in self.regime_slices],
            "symbol_cluster_slices": [s.model_dump() for s in self.symbol_cluster_slices],
            "baseline_delta": self.baseline_delta.model_dump() if self.baseline_delta else None,
            "calibration_summary": self.calibration_summary.model_dump() if self.calibration_summary else None,
            "decay_indicator": self.decay_indicator.model_dump() if self.decay_indicator else None,
        }

    def _is_flagged(self) -> bool:
        """True if the model is stale or decayed (pushed to bottom of ranking)."""
        if self.decay_indicator is not None:
            return self.decay_indicator.is_stale or self.decay_indicator.is_decayed
        return False

    def _horizon_score(self, horizon: str) -> float:
        """Get the score for a specific horizon (0.0 if not found)."""
        for sl in self.horizon_slices:
            if sl.horizon == horizon:
                return sl.score
        return 0.0

    def _regime_score(self, regime: str) -> float:
        """Get the score for a specific regime (0.0 if not found)."""
        for sl in self.regime_slices:
            if sl.regime == regime:
                return sl.score
        return 0.0

    def _cluster_score(self, cluster: str) -> float:
        """Get the score for a specific symbol cluster (0.0 if not found)."""
        for sl in self.symbol_cluster_slices:
            if sl.cluster == cluster:
                return sl.score
        return 0.0


# ---------------------------------------------------------------------------
# Leaderboard explanation
# ---------------------------------------------------------------------------


class LeaderboardExplanation(BaseModel):
    """Explanation of why a model ranks where it does.

    Frozen + extra='forbid'. Carries the model_id, rank, total_score,
    baseline delta, decay indicator, and per-horizon/regime/cluster scores.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    rank: int
    total_score: float
    baseline_delta: BaselineDelta | None = None
    decay_indicator: DecayIndicator | None = None
    horizon_scores: dict[str, float] = {}
    regime_scores: dict[str, float] = {}
    symbol_cluster_scores: dict[str, float] = {}
    is_stale: bool = False
    is_decayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for audit/persistence."""
        return {
            "model_id": self.model_id,
            "rank": self.rank,
            "total_score": self.total_score,
            "baseline_delta": self.baseline_delta.model_dump() if self.baseline_delta else None,
            "decay_indicator": self.decay_indicator.model_dump() if self.decay_indicator else None,
            "horizon_scores": dict(self.horizon_scores),
            "regime_scores": dict(self.regime_scores),
            "symbol_cluster_scores": dict(self.symbol_cluster_scores),
            "is_stale": self.is_stale,
            "is_decayed": self.is_decayed,
        }


# ---------------------------------------------------------------------------
# The expanded leaderboard
# ===========================================================================


class ExpandedLeaderboard:
    """Expanded leaderboard with horizon/regime/cluster slices + decay flags.

    Models are sorted by:
    1. flagged status (non-flagged first, stale/decayed pushed to bottom).
    2. total_score (descending) within the same flagged status.

    The leaderboard also supports per-horizon, per-regime, and per-cluster
    rankings, and can explain why a model ranks where it does.
    """

    def __init__(self) -> None:
        self._entries: list[ExpandedLeaderboardEntry] = []

    def add(self, entry: ExpandedLeaderboardEntry) -> None:
        """Add an entry to the leaderboard."""
        self._entries.append(entry)

    def ranked(self) -> list[ExpandedLeaderboardEntry]:
        """Return entries ranked best-first.

        Non-flagged (non-stale, non-decayed) models come first (sorted by
        score descending). Flagged models are pushed to the bottom.
        """
        return sorted(
            self._entries,
            key=lambda e: (
                1 if e._is_flagged() else 0,  # non-flagged first
                -e.total_score,
            ),
        )

    def ranked_by_horizon(self, horizon: str) -> list[ExpandedLeaderboardEntry]:
        """Return entries ranked by score for a specific horizon."""
        return sorted(
            self._entries,
            key=lambda e: (
                1 if e._is_flagged() else 0,
                -e._horizon_score(horizon),
            ),
        )

    def ranked_by_regime(self, regime: str) -> list[ExpandedLeaderboardEntry]:
        """Return entries ranked by score for a specific regime."""
        return sorted(
            self._entries,
            key=lambda e: (
                1 if e._is_flagged() else 0,
                -e._regime_score(regime),
            ),
        )

    def ranked_by_symbol_cluster(self, cluster: str) -> list[ExpandedLeaderboardEntry]:
        """Return entries ranked by score for a specific symbol cluster."""
        return sorted(
            self._entries,
            key=lambda e: (
                1 if e._is_flagged() else 0,
                -e._cluster_score(cluster),
            ),
        )

    def stale_models(self) -> list[ExpandedLeaderboardEntry]:
        """Return all stale models."""
        return [
            e for e in self._entries
            if e.decay_indicator is not None and e.decay_indicator.is_stale
        ]

    def decayed_models(self) -> list[ExpandedLeaderboardEntry]:
        """Return all decayed models."""
        return [
            e for e in self._entries
            if e.decay_indicator is not None and e.decay_indicator.is_decayed
        ]

    def explain(self, model_id: str) -> LeaderboardExplanation:
        """Explain why a model ranks where it does.

        Returns a ``LeaderboardExplanation`` with the model's rank, score,
        baseline delta, decay indicator, and per-horizon/regime/cluster scores.
        """
        ranked = self.ranked()
        for rank, entry in enumerate(ranked, start=1):
            if entry.model_id == model_id:
                return LeaderboardExplanation(
                    model_id=entry.model_id,
                    rank=rank,
                    total_score=entry.total_score,
                    baseline_delta=entry.baseline_delta,
                    decay_indicator=entry.decay_indicator,
                    horizon_scores={
                        s.horizon: s.score for s in entry.horizon_slices
                    },
                    regime_scores={
                        s.regime: s.score for s in entry.regime_slices
                    },
                    symbol_cluster_scores={
                        s.cluster: s.score for s in entry.symbol_cluster_slices
                    },
                    is_stale=(
                        entry.decay_indicator.is_stale
                        if entry.decay_indicator
                        else False
                    ),
                    is_decayed=(
                        entry.decay_indicator.is_decayed
                        if entry.decay_indicator
                        else False
                    ),
                )
        raise KeyError(f"model {model_id!r} not found in leaderboard")

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for audit/persistence."""
        return {
            "ranked": [e.to_dict() for e in self.ranked()],
        }
