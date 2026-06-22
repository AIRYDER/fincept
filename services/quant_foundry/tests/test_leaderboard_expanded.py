"""
Tests for TASK-0701: Expand Tournament Leaderboards.

TDD red phase — these tests are written BEFORE the implementation and must
fail with ModuleNotFoundError / ImportError until `leaderboard_expanded.py`
exists.

Acceptance criteria covered:
- A model can rank high in one horizon and low in another.
- Stale or decayed models are flagged.
- Leaderboard explains why a model ranks where it does.

Additional checks from the spec:
- Horizon slices, regime slices, symbol-cluster slices, event/news-type slices.
- Baseline deltas.
- Confidence calibration summaries.
- Decay indicators.

File-disjoint from my `leaderboard.py` + `tournament.py` (read-only imports).
Does NOT modify them (avoids breaking existing TASK-0404 tests).
"""

from __future__ import annotations

from typing import Any

import pytest
from quant_foundry.leaderboard_expanded import (
    BaselineDelta,
    CalibrationSummary,
    DecayIndicator,
    ExpandedLeaderboard,
    ExpandedLeaderboardEntry,
    HorizonSlice,
    LeaderboardExplanation,
    RegimeSlice,
    SymbolClusterSlice,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(
    model_id: str = "m1",
    total_score: float = 0.8,
    horizon_scores: dict[str, float] | None = None,
    regime_scores: dict[str, float] | None = None,
    symbol_cluster_scores: dict[str, float] | None = None,
    baseline_delta: float = 0.1,
    brier_score: float = 0.15,
    decay_score: float = 0.0,
    is_stale: bool = False,
    is_decayed: bool = False,
) -> ExpandedLeaderboardEntry:
    """Build a minimal expanded leaderboard entry for testing."""
    if horizon_scores is None:
        horizon_scores = {"1h": 0.8, "4h": 0.6, "1d": 0.4}
    if regime_scores is None:
        regime_scores = {"trending": 0.9, "ranging": 0.3}
    if symbol_cluster_scores is None:
        symbol_cluster_scores = {"tech": 0.7, "energy": 0.5}
    return ExpandedLeaderboardEntry(
        model_id=model_id,
        total_score=total_score,
        horizon_slices=[
            HorizonSlice(horizon=h, score=s) for h, s in horizon_scores.items()
        ],
        regime_slices=[
            RegimeSlice(regime=r, score=s) for r, s in regime_scores.items()
        ],
        symbol_cluster_slices=[
            SymbolClusterSlice(cluster=c, score=s)
            for c, s in symbol_cluster_scores.items()
        ],
        baseline_delta=BaselineDelta(
            baseline_model_id="baseline",
            delta=baseline_delta,
            baseline_score=total_score - baseline_delta,
        ),
        calibration_summary=CalibrationSummary(
            brier_score=brier_score,
            reliability=0.85,
            n_bins=10,
        ),
        decay_indicator=DecayIndicator(
            decay_score=decay_score,
            is_stale=is_stale,
            is_decayed=is_decayed,
            days_since_last_settlement=30 if is_stale else 1,
        ),
    )


# ---------------------------------------------------------------------------
# Slice schemas
# ===========================================================================


class TestHorizonSlice:
    """Horizon slices allow ranking by horizon."""

    def test_horizon_slice_has_required_fields(self) -> None:
        """HorizonSlice has horizon + score."""
        sl = HorizonSlice(horizon="1h", score=0.8)
        assert sl.horizon == "1h"
        assert sl.score == 0.8

    def test_horizon_slice_is_frozen(self) -> None:
        """HorizonSlice is frozen."""
        sl = HorizonSlice(horizon="1h", score=0.8)
        with pytest.raises((TypeError, ValueError)):
            sl.score = 0.5  # type: ignore[misc]


class TestRegimeSlice:
    """Regime slices allow ranking by regime."""

    def test_regime_slice_has_required_fields(self) -> None:
        """RegimeSlice has regime + score."""
        sl = RegimeSlice(regime="trending", score=0.9)
        assert sl.regime == "trending"
        assert sl.score == 0.9


class TestSymbolClusterSlice:
    """Symbol-cluster slices allow ranking by cluster."""

    def test_symbol_cluster_slice_has_required_fields(self) -> None:
        """SymbolClusterSlice has cluster + score."""
        sl = SymbolClusterSlice(cluster="tech", score=0.7)
        assert sl.cluster == "tech"
        assert sl.score == 0.7


# ---------------------------------------------------------------------------
# Baseline delta
# ===========================================================================


class TestBaselineDelta:
    """Baseline deltas show how a model compares to the baseline."""

    def test_baseline_delta_has_required_fields(self) -> None:
        """BaselineDelta has baseline_model_id, delta, baseline_score."""
        bd = BaselineDelta(
            baseline_model_id="baseline",
            delta=0.1,
            baseline_score=0.7,
        )
        assert bd.baseline_model_id == "baseline"
        assert bd.delta == 0.1
        assert bd.baseline_score == 0.7

    def test_positive_delta_means_model_beats_baseline(self) -> None:
        """A positive delta means the model beats the baseline."""
        bd = BaselineDelta(baseline_model_id="b", delta=0.1, baseline_score=0.7)
        assert bd.delta > 0  # model beats baseline


# ---------------------------------------------------------------------------
# Calibration summary
# ===========================================================================


class TestCalibrationSummary:
    """Confidence calibration summaries."""

    def test_calibration_summary_has_required_fields(self) -> None:
        """CalibrationSummary has brier_score, reliability, n_bins."""
        cs = CalibrationSummary(brier_score=0.15, reliability=0.85, n_bins=10)
        assert cs.brier_score == 0.15
        assert cs.reliability == 0.85
        assert cs.n_bins == 10


# ---------------------------------------------------------------------------
# Decay indicator
# ===========================================================================


class TestDecayIndicator:
    """Decay indicators flag stale or decayed models."""

    def test_decay_indicator_has_required_fields(self) -> None:
        """DecayIndicator has decay_score, is_stale, is_decayed, days_since_last_settlement."""
        di = DecayIndicator(
            decay_score=0.3,
            is_stale=True,
            is_decayed=False,
            days_since_last_settlement=30,
        )
        assert di.decay_score == 0.3
        assert di.is_stale is True
        assert di.is_decayed is False
        assert di.days_since_last_settlement == 30

    def test_stale_model_is_flagged(self) -> None:
        """A stale model is flagged."""
        di = DecayIndicator(
            decay_score=0.0, is_stale=True, is_decayed=False,
            days_since_last_settlement=60,
        )
        assert di.is_stale is True

    def test_decayed_model_is_flagged(self) -> None:
        """A decayed model is flagged."""
        di = DecayIndicator(
            decay_score=0.5, is_stale=False, is_decayed=True,
            days_since_last_settlement=5,
        )
        assert di.is_decayed is True


# ---------------------------------------------------------------------------
# ExpandedLeaderboardEntry
# ===========================================================================


class TestExpandedLeaderboardEntry:
    """An expanded leaderboard entry with slices + deltas + calibration + decay."""

    def test_entry_has_required_fields(self) -> None:
        """Entry has model_id, total_score, slices, baseline_delta, calibration, decay."""
        entry = _make_entry()
        assert entry.model_id == "m1"
        assert entry.total_score == 0.8
        assert len(entry.horizon_slices) > 0
        assert len(entry.regime_slices) > 0
        assert len(entry.symbol_cluster_slices) > 0
        assert isinstance(entry.baseline_delta, BaselineDelta)
        assert isinstance(entry.calibration_summary, CalibrationSummary)
        assert isinstance(entry.decay_indicator, DecayIndicator)

    def test_entry_to_dict_is_json_serializable(self) -> None:
        """Entry can be serialized to JSON."""
        import json

        entry = _make_entry()
        d = entry.to_dict()
        json.dumps(d)
        assert "model_id" in d
        assert "horizon_slices" in d
        assert "baseline_delta" in d
        assert "calibration_summary" in d
        assert "decay_indicator" in d


# ---------------------------------------------------------------------------
# ExpandedLeaderboard — a model can rank high in one horizon and low in another
# ===========================================================================


class TestHorizonRanking:
    """A model can rank high in one horizon and low in another."""

    def test_rank_by_horizon(self) -> None:
        """Ranking by horizon produces different rankings for different horizons."""
        m1 = _make_entry(
            model_id="m1",
            horizon_scores={"1h": 0.9, "4h": 0.3, "1d": 0.2},
        )
        m2 = _make_entry(
            model_id="m2",
            horizon_scores={"1h": 0.3, "4h": 0.9, "1d": 0.8},
        )
        lb = ExpandedLeaderboard()
        lb.add(m1)
        lb.add(m2)

        # In the 1h horizon, m1 should rank higher.
        ranked_1h = lb.ranked_by_horizon("1h")
        assert ranked_1h[0].model_id == "m1"

        # In the 4h horizon, m2 should rank higher.
        ranked_4h = lb.ranked_by_horizon("4h")
        assert ranked_4h[0].model_id == "m2"

    def test_rank_by_regime(self) -> None:
        """Ranking by regime produces different rankings for different regimes."""
        m1 = _make_entry(
            model_id="m1",
            regime_scores={"trending": 0.9, "ranging": 0.2},
        )
        m2 = _make_entry(
            model_id="m2",
            regime_scores={"trending": 0.3, "ranging": 0.9},
        )
        lb = ExpandedLeaderboard()
        lb.add(m1)
        lb.add(m2)

        ranked_trending = lb.ranked_by_regime("trending")
        assert ranked_trending[0].model_id == "m1"

        ranked_ranging = lb.ranked_by_regime("ranging")
        assert ranked_ranging[0].model_id == "m2"

    def test_rank_by_symbol_cluster(self) -> None:
        """Ranking by symbol cluster produces different rankings for different clusters."""
        m1 = _make_entry(
            model_id="m1",
            symbol_cluster_scores={"tech": 0.9, "energy": 0.2},
        )
        m2 = _make_entry(
            model_id="m2",
            symbol_cluster_scores={"tech": 0.3, "energy": 0.9},
        )
        lb = ExpandedLeaderboard()
        lb.add(m1)
        lb.add(m2)

        ranked_tech = lb.ranked_by_symbol_cluster("tech")
        assert ranked_tech[0].model_id == "m1"

        ranked_energy = lb.ranked_by_symbol_cluster("energy")
        assert ranked_energy[0].model_id == "m2"


# ---------------------------------------------------------------------------
# Stale or decayed models are flagged
# ===========================================================================


class TestStaleDecayedFlagging:
    """Stale or decayed models are flagged and pushed to the bottom."""

    def test_stale_model_is_flagged_in_ranking(self) -> None:
        """A stale model is flagged and pushed to the bottom of the ranking."""
        m1 = _make_entry(model_id="m1", total_score=0.5, is_stale=False)
        m2 = _make_entry(model_id="m2", total_score=0.9, is_stale=True)
        lb = ExpandedLeaderboard()
        lb.add(m1)
        lb.add(m2)

        # Even though m2 has a higher score, it's stale and should be ranked lower.
        ranked = lb.ranked()
        assert ranked[0].model_id == "m1"
        assert ranked[1].model_id == "m2"

    def test_decayed_model_is_flagged_in_ranking(self) -> None:
        """A decayed model is flagged and pushed to the bottom of the ranking."""
        m1 = _make_entry(model_id="m1", total_score=0.5, is_decayed=False)
        m2 = _make_entry(model_id="m2", total_score=0.9, is_decayed=True)
        lb = ExpandedLeaderboard()
        lb.add(m1)
        lb.add(m2)

        ranked = lb.ranked()
        assert ranked[0].model_id == "m1"
        assert ranked[1].model_id == "m2"

    def test_stale_models_list(self) -> None:
        """The leaderboard can list all stale models."""
        m1 = _make_entry(model_id="m1", is_stale=False)
        m2 = _make_entry(model_id="m2", is_stale=True)
        m3 = _make_entry(model_id="m3", is_stale=True)
        lb = ExpandedLeaderboard()
        lb.add(m1)
        lb.add(m2)
        lb.add(m3)

        stale = lb.stale_models()
        assert len(stale) == 2
        stale_ids = {m.model_id for m in stale}
        assert stale_ids == {"m2", "m3"}

    def test_decayed_models_list(self) -> None:
        """The leaderboard can list all decayed models."""
        m1 = _make_entry(model_id="m1", is_decayed=False)
        m2 = _make_entry(model_id="m2", is_decayed=True)
        lb = ExpandedLeaderboard()
        lb.add(m1)
        lb.add(m2)

        decayed = lb.decayed_models()
        assert len(decayed) == 1
        assert decayed[0].model_id == "m2"


# ---------------------------------------------------------------------------
# Leaderboard explains why a model ranks where it does
# ===========================================================================


class TestLeaderboardExplanation:
    """Leaderboard explains why a model ranks where it does."""

    def test_explain_returns_explanation(self) -> None:
        """explain() returns a LeaderboardExplanation for a model."""
        m1 = _make_entry(model_id="m1", total_score=0.8)
        lb = ExpandedLeaderboard()
        lb.add(m1)

        explanation = lb.explain("m1")
        assert isinstance(explanation, LeaderboardExplanation)
        assert explanation.model_id == "m1"

    def test_explanation_includes_rank(self) -> None:
        """The explanation includes the model's rank."""
        m1 = _make_entry(model_id="m1", total_score=0.8)
        m2 = _make_entry(model_id="m2", total_score=0.6)
        lb = ExpandedLeaderboard()
        lb.add(m1)
        lb.add(m2)

        explanation = lb.explain("m1")
        assert explanation.rank == 1

    def test_explanation_includes_score_components(self) -> None:
        """The explanation includes the score breakdown."""
        m1 = _make_entry(model_id="m1", total_score=0.8)
        lb = ExpandedLeaderboard()
        lb.add(m1)

        explanation = lb.explain("m1")
        assert hasattr(explanation, "total_score")
        assert explanation.total_score == 0.8

    def test_explanation_includes_baseline_delta(self) -> None:
        """The explanation includes the baseline delta."""
        m1 = _make_entry(model_id="m1", baseline_delta=0.1)
        lb = ExpandedLeaderboard()
        lb.add(m1)

        explanation = lb.explain("m1")
        assert hasattr(explanation, "baseline_delta")
        assert explanation.baseline_delta.delta == 0.1

    def test_explanation_includes_decay_indicator(self) -> None:
        """The explanation includes the decay indicator."""
        m1 = _make_entry(model_id="m1", is_stale=True)
        lb = ExpandedLeaderboard()
        lb.add(m1)

        explanation = lb.explain("m1")
        assert hasattr(explanation, "decay_indicator")
        assert explanation.decay_indicator.is_stale is True

    def test_explanation_includes_horizon_scores(self) -> None:
        """The explanation includes horizon scores."""
        m1 = _make_entry(model_id="m1", horizon_scores={"1h": 0.9, "4h": 0.3})
        lb = ExpandedLeaderboard()
        lb.add(m1)

        explanation = lb.explain("m1")
        assert hasattr(explanation, "horizon_scores")
        assert explanation.horizon_scores["1h"] == 0.9

    def test_explanation_to_dict_is_json_serializable(self) -> None:
        """The explanation can be serialized to JSON."""
        import json

        m1 = _make_entry(model_id="m1")
        lb = ExpandedLeaderboard()
        lb.add(m1)

        explanation = lb.explain("m1")
        d = explanation.to_dict()
        json.dumps(d)
        assert "model_id" in d
        assert "rank" in d


# ---------------------------------------------------------------------------
# Leaderboard to_dict
# ===========================================================================


class TestExpandedLeaderboardToDict:
    """The expanded leaderboard can be serialized."""

    def test_to_dict_is_json_serializable(self) -> None:
        """The leaderboard can be serialized to JSON."""
        import json

        m1 = _make_entry(model_id="m1")
        lb = ExpandedLeaderboard()
        lb.add(m1)

        d = lb.to_dict()
        json.dumps(d)
        assert "ranked" in d
        assert len(d["ranked"]) > 0


# ---------------------------------------------------------------------------
# No secrets in output
# ===========================================================================


class TestNoSecretsInExpandedLeaderboard:
    """Expanded leaderboard output must not leak secrets."""

    def test_to_dict_has_no_secret_keys(self) -> None:

        m1 = _make_entry(model_id="m1")
        lb = ExpandedLeaderboard()
        lb.add(m1)

        d = lb.to_dict()

        def _has_secret(d: Any, secret_names: set[str]) -> bool:
            if isinstance(d, dict):
                for k, v in d.items():
                    if k.lower() in secret_names:
                        return True
                    if _has_secret(v, secret_names):
                        return True
            elif isinstance(d, list):
                for item in d:
                    if _has_secret(item, secret_names):
                        return True
            return False

        secret_names = {"api_key", "token", "secret", "password",
                        "broker_account", "credential"}
        assert not _has_secret(d, secret_names)
