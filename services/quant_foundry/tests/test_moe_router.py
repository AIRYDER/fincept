"""
Tests for TASK-1001: Mixture-of-Experts Model Router.

TDD red phase — these tests are written BEFORE the implementation and must
fail with ModuleNotFoundError / ImportError until `moe_router.py` exists.

Acceptance criteria covered (from spec):
- Learn which model to trust by regime, symbol, liquidity, volatility,
  news type, horizon, feature availability, and recent calibration.
- Start with rules from tournament evidence.
- Add learned router only after enough settled data exists.
- Add abstain output.

File-disjoint from my leaderboard_expanded.py + tournament.py +
shadow_inference.py (read-only imports). Does NOT modify them.
"""

from __future__ import annotations

from typing import Any

import pytest
from quant_foundry.leaderboard_expanded import (
    BaselineDelta,
    CalibrationSummary,
    DecayIndicator,
    ExpandedLeaderboardEntry,
    HorizonSlice,
    RegimeSlice,
    SymbolClusterSlice,
)
from quant_foundry.moe_router import (
    AbstainReason,
    ExpertWeight,
    MoERouter,
    MoERouterConfig,
    RoutingContext,
    RoutingDecision,
    RoutingRule,
    route_prediction,
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
    brier_score: float = 0.15,
    baseline_delta: float = 0.1,
    decay_score: float = 0.0,
    is_stale: bool = False,
    is_decayed: bool = False,
    settled_count: int = 100,
) -> ExpandedLeaderboardEntry:
    if horizon_scores is None:
        horizon_scores = {"1h": 0.8, "4h": 0.6, "1d": 0.4}
    if regime_scores is None:
        regime_scores = {"trending": 0.9, "ranging": 0.3}
    if symbol_cluster_scores is None:
        symbol_cluster_scores = {"tech": 0.7, "energy": 0.5}
    return ExpandedLeaderboardEntry(
        model_id=model_id,
        total_score=total_score,
        settled_count=settled_count,
        horizon_slices=[HorizonSlice(horizon=h, score=s) for h, s in horizon_scores.items()],
        regime_slices=[RegimeSlice(regime=r, score=s) for r, s in regime_scores.items()],
        symbol_cluster_slices=[
            SymbolClusterSlice(cluster=c, score=s) for c, s in symbol_cluster_scores.items()
        ],
        baseline_delta=BaselineDelta(
            baseline_model_id="baseline",
            delta=baseline_delta,
            baseline_score=total_score - baseline_delta,
        ),
        calibration_summary=CalibrationSummary(
            brier_score=brier_score, reliability=0.85, n_bins=10
        ),
        decay_indicator=DecayIndicator(
            decay_score=decay_score,
            is_stale=is_stale,
            is_decayed=is_decayed,
            days_since_last_settlement=1,
        ),
    )


def _make_context(
    regime: str = "trending",
    symbol: str = "AAPL",
    symbol_cluster: str = "tech",
    horizon: str = "1h",
    feature_availability: float = 0.9,
    liquidity: float = 0.8,
    volatility: float = 0.3,
    news_type: str = "earnings",
) -> RoutingContext:
    return RoutingContext(
        regime=regime,
        symbol=symbol,
        symbol_cluster=symbol_cluster,
        horizon=horizon,
        feature_availability=feature_availability,
        liquidity=liquidity,
        volatility=volatility,
        news_type=news_type,
    )


# ---------------------------------------------------------------------------
# RoutingContext
# ===========================================================================


class TestRoutingContext:
    """The routing context describes the current market state."""

    def test_context_has_required_fields(self) -> None:
        """Context has regime, symbol, symbol_cluster, horizon, etc."""
        ctx = _make_context()
        assert ctx.regime == "trending"
        assert ctx.symbol == "AAPL"
        assert ctx.symbol_cluster == "tech"
        assert ctx.horizon == "1h"
        assert ctx.feature_availability == 0.9
        assert ctx.liquidity == 0.8
        assert ctx.volatility == 0.3
        assert ctx.news_type == "earnings"

    def test_context_is_frozen(self) -> None:
        """Context is frozen."""
        ctx = _make_context()
        with pytest.raises((TypeError, ValueError)):
            ctx.regime = "ranging"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# MoERouterConfig
# ===========================================================================


class TestMoERouterConfig:
    """The router configuration."""

    def test_config_has_required_fields(self) -> None:
        """Config has min_settled_count, min_feature_availability, etc."""
        config = MoERouterConfig(
            min_settled_count=50,
            min_feature_availability=0.8,
            max_brier_score=0.25,
        )
        assert config.min_settled_count == 50
        assert config.min_feature_availability == 0.8
        assert config.max_brier_score == 0.25

    def test_config_defaults_are_reasonable(self) -> None:
        """Config has reasonable defaults."""
        config = MoERouterConfig()
        assert config.min_settled_count > 0
        assert config.min_feature_availability > 0
        assert config.max_brier_score > 0

    def test_config_is_frozen(self) -> None:
        """Config is frozen."""
        config = MoERouterConfig()
        with pytest.raises((TypeError, ValueError)):
            config.min_settled_count = 100  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ExpertWeight
# ===========================================================================


class TestExpertWeight:
    """An expert weight for a model in the MoE."""

    def test_weight_has_required_fields(self) -> None:
        """ExpertWeight has model_id, weight, reason."""
        w = ExpertWeight(model_id="m1", weight=0.6, reason="high score in trending regime")
        assert w.model_id == "m1"
        assert w.weight == 0.6
        assert w.reason == "high score in trending regime"

    def test_weight_is_frozen(self) -> None:
        """ExpertWeight is frozen."""
        w = ExpertWeight(model_id="m1", weight=0.6, reason="test")
        with pytest.raises((TypeError, ValueError)):
            w.weight = 0.9  # type: ignore[misc]


# ---------------------------------------------------------------------------
# RoutingDecision
# ===========================================================================


class TestRoutingDecision:
    """A routing decision with weights or abstain."""

    def test_decision_has_required_fields(self) -> None:
        """RoutingDecision has weights, is_abstain, abstain_reason."""
        decision = RoutingDecision(
            weights=[ExpertWeight(model_id="m1", weight=1.0, reason="best")],
            is_abstain=False,
        )
        assert len(decision.weights) > 0
        assert decision.is_abstain is False
        assert decision.abstain_reason is None

    def test_abstain_decision(self) -> None:
        """An abstain decision has no weights and a reason."""
        decision = RoutingDecision(
            weights=[],
            is_abstain=True,
            abstain_reason=AbstainReason.INSUFFICIENT_EVIDENCE,
        )
        assert decision.is_abstain is True
        assert len(decision.weights) == 0
        assert decision.abstain_reason == AbstainReason.INSUFFICIENT_EVIDENCE

    def test_decision_weights_sum_to_one(self) -> None:
        """Non-abstain decision weights sum to 1.0."""
        decision = RoutingDecision(
            weights=[
                ExpertWeight(model_id="m1", weight=0.6, reason="best"),
                ExpertWeight(model_id="m2", weight=0.4, reason="second"),
            ],
            is_abstain=False,
        )
        total = sum(w.weight for w in decision.weights)
        assert abs(total - 1.0) < 0.01

    def test_decision_to_dict_is_json_serializable(self) -> None:
        """Decision can be serialized to JSON."""
        import json

        decision = RoutingDecision(
            weights=[ExpertWeight(model_id="m1", weight=1.0, reason="best")],
            is_abstain=False,
        )
        d = decision.to_dict()
        json.dumps(d)
        assert "weights" in d
        assert "is_abstain" in d


# ---------------------------------------------------------------------------
# AbstainReason
# ===========================================================================


class TestAbstainReason:
    """Abstain reasons for the router."""

    def test_abstain_reasons_are_defined(self) -> None:
        """AbstainReason has the expected values."""
        assert AbstainReason.INSUFFICIENT_EVIDENCE is not None
        assert AbstainReason.LOW_FEATURE_AVAILABILITY is not None
        assert AbstainReason.POOR_CALIBRATION is not None
        assert AbstainReason.STALE_MODEL is not None
        assert AbstainReason.NO_EXPERTS is not None


# ---------------------------------------------------------------------------
# MoERouter — start with rules from tournament evidence
# ===========================================================================


class TestRuleBasedRouting:
    """Start with rules from tournament evidence."""

    def test_router_routes_to_best_model(self) -> None:
        """The router routes to the best model for the context."""
        m1 = _make_entry(model_id="m1", regime_scores={"trending": 0.9})
        m2 = _make_entry(model_id="m2", regime_scores={"trending": 0.3})
        router = MoERouter(entries=[m1, m2])
        ctx = _make_context(regime="trending")
        decision = router.route(ctx)
        assert not decision.is_abstain
        assert decision.weights[0].model_id == "m1"

    def test_router_weights_reflect_scores(self) -> None:
        """The router weights reflect the relative scores."""
        m1 = _make_entry(model_id="m1", regime_scores={"trending": 0.9})
        m2 = _make_entry(model_id="m2", regime_scores={"trending": 0.6})
        router = MoERouter(entries=[m1, m2])
        ctx = _make_context(regime="trending")
        decision = router.route(ctx)
        # m1 should have a higher weight than m2.
        weights = {w.model_id: w.weight for w in decision.weights}
        assert weights["m1"] > weights["m2"]

    def test_router_routes_by_horizon(self) -> None:
        """The router considers horizon when routing."""
        m1 = _make_entry(model_id="m1", horizon_scores={"1h": 0.9, "4h": 0.3})
        m2 = _make_entry(model_id="m2", horizon_scores={"1h": 0.3, "4h": 0.9})
        router = MoERouter(entries=[m1, m2])

        ctx_1h = _make_context(horizon="1h")
        decision_1h = router.route(ctx_1h)
        assert decision_1h.weights[0].model_id == "m1"

        ctx_4h = _make_context(horizon="4h")
        decision_4h = router.route(ctx_4h)
        assert decision_4h.weights[0].model_id == "m2"

    def test_router_routes_by_symbol_cluster(self) -> None:
        """The router considers symbol cluster when routing."""
        m1 = _make_entry(model_id="m1", symbol_cluster_scores={"tech": 0.9})
        m2 = _make_entry(model_id="m2", symbol_cluster_scores={"tech": 0.3})
        router = MoERouter(entries=[m1, m2])
        ctx = _make_context(symbol_cluster="tech")
        decision = router.route(ctx)
        assert decision.weights[0].model_id == "m1"


# ---------------------------------------------------------------------------
# Add abstain output
# ===========================================================================


class TestAbstainOutput:
    """Add abstain output."""

    def test_router_abstains_when_no_experts(self) -> None:
        """The router abstains when there are no experts."""
        router = MoERouter(entries=[])
        ctx = _make_context()
        decision = router.route(ctx)
        assert decision.is_abstain
        assert decision.abstain_reason == AbstainReason.NO_EXPERTS

    def test_router_abstains_on_low_feature_availability(self) -> None:
        """The router abstains when feature availability is too low."""
        m1 = _make_entry(model_id="m1")
        router = MoERouter(entries=[m1])
        ctx = _make_context(feature_availability=0.3)
        decision = router.route(ctx)
        assert decision.is_abstain
        assert decision.abstain_reason == AbstainReason.LOW_FEATURE_AVAILABILITY

    def test_router_abstains_on_stale_model(self) -> None:
        """The router abstains when the only model is stale."""
        m1 = _make_entry(model_id="m1", is_stale=True)
        router = MoERouter(entries=[m1])
        ctx = _make_context()
        decision = router.route(ctx)
        assert decision.is_abstain
        assert decision.abstain_reason == AbstainReason.STALE_MODEL

    def test_router_abstains_on_poor_calibration(self) -> None:
        """The router abstains when calibration is poor."""
        m1 = _make_entry(model_id="m1", brier_score=0.5)
        router = MoERouter(entries=[m1])
        ctx = _make_context()
        decision = router.route(ctx)
        assert decision.is_abstain
        assert decision.abstain_reason == AbstainReason.POOR_CALIBRATION


# ---------------------------------------------------------------------------
# Add learned router only after enough settled data exists
# ===========================================================================


class TestLearnedRouterGate:
    """Add learned router only after enough settled data exists."""

    def test_router_abstains_with_insufficient_evidence(self) -> None:
        """The router abstains when there's insufficient settled evidence."""
        m1 = _make_entry(model_id="m1", settled_count=5)
        config = MoERouterConfig(min_settled_count=50)
        router = MoERouter(entries=[m1], config=config)
        ctx = _make_context()
        decision = router.route(ctx)
        assert decision.is_abstain
        assert decision.abstain_reason == AbstainReason.INSUFFICIENT_EVIDENCE

    def test_router_routes_with_sufficient_evidence(self) -> None:
        """The router routes when there's sufficient settled evidence."""
        m1 = _make_entry(model_id="m1", settled_count=100)
        config = MoERouterConfig(min_settled_count=50)
        router = MoERouter(entries=[m1], config=config)
        ctx = _make_context()
        decision = router.route(ctx)
        assert not decision.is_abstain


# ---------------------------------------------------------------------------
# RoutingRule
# ===========================================================================


class TestRoutingRule:
    """A routing rule for the MoE."""

    def test_rule_has_required_fields(self) -> None:
        """RoutingRule has name, condition, weight_modifier."""
        rule = RoutingRule(
            name="prefer_trending_experts",
            condition="regime == 'trending'",
            weight_modifier=1.5,
        )
        assert rule.name == "prefer_trending_experts"
        assert rule.condition == "regime == 'trending'"
        assert rule.weight_modifier == 1.5


# ---------------------------------------------------------------------------
# Convenience function
# ===========================================================================


class TestRoutePrediction:
    """The convenience function route_prediction works."""

    def test_route_prediction_returns_decision(self) -> None:
        """route_prediction returns a RoutingDecision."""
        m1 = _make_entry(model_id="m1")
        ctx = _make_context()
        decision = route_prediction(entries=[m1], context=ctx)
        assert isinstance(decision, RoutingDecision)
        assert not decision.is_abstain


# ---------------------------------------------------------------------------
# No secrets in output
# ===========================================================================


class TestNoSecretsInMoEOutput:
    """MoE router output must not leak secrets."""

    def test_decision_to_dict_has_no_secret_keys(self) -> None:

        m1 = _make_entry(model_id="m1")
        router = MoERouter(entries=[m1])
        ctx = _make_context()
        decision = router.route(ctx)
        d = decision.to_dict()

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

        secret_names = {"api_key", "token", "secret", "password", "broker_account", "credential"}
        assert not _has_secret(d, secret_names)
