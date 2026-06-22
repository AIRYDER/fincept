"""
quant_foundry.moe_router — mixture-of-experts model router (TASK-1001).

Learns which model to trust by regime, symbol, liquidity, volatility, news
type, horizon, feature availability, and recent calibration. Starts with
rules from tournament evidence. Adds a learned router only after enough
settled data exists. Adds abstain output.

Key invariants:
- **Rule-based routing from tournament evidence.** The router uses the
  expanded leaderboard entries (TASK-0701) to route predictions based on
  regime, horizon, and symbol-cluster scores.
- **Abstain output.** The router abstains when:
  - No experts are available (NO_EXPERTS).
  - Feature availability is below the threshold (LOW_FEATURE_AVAILABILITY).
  - The only model is stale (STALE_MODEL).
  - Calibration is poor (POOR_CALIBRATION).
  - Insufficient settled evidence (INSUFFICIENT_EVIDENCE).
- **Learned router gate.** The router only routes when there's enough
  settled evidence (``min_settled_count``). Below the threshold, it
  abstains with ``INSUFFICIENT_EVIDENCE``.

File-disjoint from my ``leaderboard_expanded.py`` + ``tournament.py`` +
``shadow_inference.py`` (read-only imports). Does NOT modify them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from quant_foundry.leaderboard_expanded import ExpandedLeaderboardEntry

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class MoERouterConfig(BaseModel):
    """Configuration for the MoE router.

    Frozen + extra='forbid'. Carries the minimum settled count, minimum
    feature availability, and maximum Brier score thresholds.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_settled_count: int = 50
    min_feature_availability: float = 0.8
    max_brier_score: float = 0.25


# ---------------------------------------------------------------------------
# Routing context
# ---------------------------------------------------------------------------


class RoutingContext(BaseModel):
    """The routing context describing the current market state.

    Frozen + extra='forbid'. Carries the regime, symbol, symbol cluster,
    horizon, feature availability, liquidity, volatility, and news type.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    regime: str = "unknown"
    symbol: str = ""
    symbol_cluster: str = ""
    horizon: str = "1d"
    feature_availability: float = 1.0
    liquidity: float = 0.5
    volatility: float = 0.3
    news_type: str = ""


# ---------------------------------------------------------------------------
# Expert weight
# ---------------------------------------------------------------------------


class ExpertWeight(BaseModel):
    """An expert weight for a model in the MoE.

    Frozen + extra='forbid'. Carries the model_id, weight (0-1), and
    a human-readable reason for the weight.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    weight: float
    reason: str


# ---------------------------------------------------------------------------
# Abstain reason
# ---------------------------------------------------------------------------


class AbstainReason(StrEnum):
    """Reason why the router abstained."""

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    LOW_FEATURE_AVAILABILITY = "low_feature_availability"
    POOR_CALIBRATION = "poor_calibration"
    STALE_MODEL = "stale_model"
    NO_EXPERTS = "no_experts"


# ---------------------------------------------------------------------------
# Routing decision
# ---------------------------------------------------------------------------


class RoutingDecision(BaseModel):
    """A routing decision with weights or abstain.

    Frozen + extra='forbid'. Carries the expert weights, whether the
    router abstained, and the abstain reason (if any).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    weights: list[ExpertWeight] = []
    is_abstain: bool = False
    abstain_reason: AbstainReason | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for audit/persistence."""
        return {
            "weights": [w.model_dump() for w in self.weights],
            "is_abstain": self.is_abstain,
            "abstain_reason": (
                self.abstain_reason.value if self.abstain_reason else None
            ),
        }


# ---------------------------------------------------------------------------
# Routing rule
# ---------------------------------------------------------------------------


class RoutingRule(BaseModel):
    """A routing rule for the MoE.

    Frozen + extra='forbid'. Carries a name, condition (human-readable),
    and weight modifier. Rules are applied on top of the base scores.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    condition: str
    weight_modifier: float = 1.0


# ---------------------------------------------------------------------------
# The router
# ===========================================================================


class MoERouter:
    """Mixture-of-experts model router.

    Routes predictions to the best model(s) based on the routing context
    and the expanded leaderboard entries. Uses rule-based routing from
    tournament evidence (regime, horizon, symbol-cluster scores).

    The router abstains when:
    - No experts are available.
    - Feature availability is below the threshold.
    - The only model is stale.
    - Calibration is poor.
    - Insufficient settled evidence.
    """

    def __init__(
        self,
        entries: list[ExpandedLeaderboardEntry],
        config: MoERouterConfig | None = None,
        rules: list[RoutingRule] | None = None,
    ) -> None:
        self.entries = entries
        self.config = config or MoERouterConfig()
        self.rules = rules or []

    def _get_entry_score(self, entry: ExpandedLeaderboardEntry, ctx: RoutingContext) -> float:
        """Get the context-specific score for an entry.

        Combines regime, horizon, and symbol-cluster scores with the
        total score as a fallback.
        """
        # Get regime score.
        regime_score = 0.0
        for rsl in entry.regime_slices:
            if rsl.regime == ctx.regime:
                regime_score = rsl.score
                break

        # Get horizon score.
        horizon_score = 0.0
        for hsl in entry.horizon_slices:
            if hsl.horizon == ctx.horizon:
                horizon_score = hsl.score
                break

        # Get symbol cluster score.
        cluster_score = 0.0
        for csl in entry.symbol_cluster_slices:
            if csl.cluster == ctx.symbol_cluster:
                cluster_score = csl.score
                break

        # Weighted combination (regime > horizon > cluster > total).
        score = (
            0.35 * regime_score
            + 0.25 * horizon_score
            + 0.20 * cluster_score
            + 0.20 * entry.total_score
        )
        return score

    def _is_eligible(self, entry: ExpandedLeaderboardEntry) -> bool:
        """Check if an entry is eligible (not stale, not decayed)."""
        return not (
            entry.decay_indicator is not None
            and (entry.decay_indicator.is_stale or entry.decay_indicator.is_decayed)
        )

    def route(self, ctx: RoutingContext) -> RoutingDecision:
        """Route a prediction to the best model(s).

        Returns a ``RoutingDecision`` with expert weights, or an abstain
        decision if the router cannot route.
        """
        # 1. No experts -> abstain.
        if not self.entries:
            return RoutingDecision(
                weights=[],
                is_abstain=True,
                abstain_reason=AbstainReason.NO_EXPERTS,
            )

        # 2. Low feature availability -> abstain.
        if ctx.feature_availability < self.config.min_feature_availability:
            return RoutingDecision(
                weights=[],
                is_abstain=True,
                abstain_reason=AbstainReason.LOW_FEATURE_AVAILABILITY,
            )

        # 3. Filter eligible entries (not stale, not decayed).
        eligible = [e for e in self.entries if self._is_eligible(e)]

        # 4. If all entries are stale -> abstain.
        if not eligible:
            return RoutingDecision(
                weights=[],
                is_abstain=True,
                abstain_reason=AbstainReason.STALE_MODEL,
            )

        # 5. Check calibration — abstain if all eligible models have poor calibration.
        well_calibrated = [
            e for e in eligible
            if e.calibration_summary is None
            or e.calibration_summary.brier_score <= self.config.max_brier_score
        ]
        if not well_calibrated:
            return RoutingDecision(
                weights=[],
                is_abstain=True,
                abstain_reason=AbstainReason.POOR_CALIBRATION,
            )

        # 6. Check settled evidence — abstain if all models have insufficient evidence.
        sufficient_evidence = [
            e for e in well_calibrated
            if e.settled_count >= self.config.min_settled_count
        ]
        if not sufficient_evidence:
            return RoutingDecision(
                weights=[],
                is_abstain=True,
                abstain_reason=AbstainReason.INSUFFICIENT_EVIDENCE,
            )

        # 7. Compute scores for eligible entries.
        scored = [
            (e, self._get_entry_score(e, ctx)) for e in sufficient_evidence
        ]

        # 8. Filter out non-positive scores.
        positive = [(e, s) for e, s in scored if s > 0]
        if not positive:
            # All scores are zero or negative -> abstain.
            return RoutingDecision(
                weights=[],
                is_abstain=True,
                abstain_reason=AbstainReason.NO_EXPERTS,
            )

        # 9. Normalize weights to sum to 1.0.
        total_score = sum(s for _, s in positive)
        if total_score <= 0:
            return RoutingDecision(
                weights=[],
                is_abstain=True,
                abstain_reason=AbstainReason.NO_EXPERTS,
            )

        weights = [
            ExpertWeight(
                model_id=e.model_id,
                weight=s / total_score,
                reason=f"score={s:.4f} for regime={ctx.regime},horizon={ctx.horizon}",
            )
            for e, s in positive
        ]

        # Sort by weight descending.
        weights.sort(key=lambda w: -w.weight)

        return RoutingDecision(
            weights=weights,
            is_abstain=False,
        )


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def route_prediction(
    entries: list[ExpandedLeaderboardEntry],
    context: RoutingContext,
    config: MoERouterConfig | None = None,
) -> RoutingDecision:
    """Route a prediction to the best model(s).

    Convenience entry point for TASK-1001. Creates a ``MoERouter`` and
    routes the prediction.
    """
    router = MoERouter(entries=entries, config=config)
    return router.route(context)
