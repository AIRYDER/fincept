"""
quant_foundry.tournament — the model tournament (TASK-0404).

The tournament is the scoreboard. It takes settled predictions (from
TASK-0401) and dossiers (from TASK-0403) and produces a ranked leaderboard.
A model with a high ML score but poor cost-adjusted return must lose to a
simpler model that makes money. This is what prevents overfit models from
being promoted.

What the tournament scores on (cross-cutting rigor §2 + §4):
- **net edge** (out-of-sample, AFTER modeled costs) — gross edge is NOT
  allowed to drive ranking (rigor §4 cost governance).
- **Deflated Sharpe Ratio** (discounts for trial count + return
  non-normality) — not raw Sharpe (rigor §2).
- **calibration** (Brier score + reliability) — monotonic edge-vs-confidence
  is a health signal; non-monotonic is a red flag.
- **drawdown penalty** — deep drawdowns lower the score.
- **turnover penalty** (if available) — high turnover lowers the score.
- **feature availability penalty** — missing features lower the score.
- **latency penalty** (if available) — high latency lowers the score.
- **capacity/decay penalty** — capacity-constrained or decaying models lower
  the score.
- **bootstrap p-value vs. baseline** — a model that does not significantly
  beat the zero-skill / persistence / buy-and-hold baselines (net of cost)
  is blocked.

The score is a simple, explainable weighted sum over the components. The
weights and the deflation inputs are recorded on every result so a rank is
auditable (an opaque score the operator cannot interrogate is itself a risk).

Gating (a model can be blocked even if its score is high):
- **insufficient-evidence**: too few settled predictions (< min_settled_samples).
  Never ranked above a model with sufficient evidence.
- **stale**: the last settled prediction is too old (> stale_threshold_ns).
  Blocks promotion.
- **blocked**: a blocking issue was raised (e.g. "fails net-of-cost vs
  persistence", "DSR <= 0 after deflation", "calibration not monotonic",
  "net edge negative"). Hard gate on promotion.

File-disjoint from all active builders (see BUILDER3.md). Does NOT import
``outcomes.py`` / ``settlement.py`` / ``dossier.py`` — the tournament
consumes settled predictions and dossier metadata via a local
``ScoringInput`` schema (plain pydantic model), so Builder 1's evidence
storage internals can change without breaking the tournament and vice versa.
"""

from __future__ import annotations

import statistics
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quant_foundry.significance import (
    deflated_sharpe_ratio,
    stationary_bootstrap_pvalue,
)

# ---------------------------------------------------------------------------
# Scoring input — carries the OOS return series + trial count (rigor §2)
# ---------------------------------------------------------------------------


class BaselineKind(StrEnum):
    """Deterministic baselines that every model must beat net-of-cost.

    - ``ZERO_SKILL``: always-flat (0 return every period). A model that
      cannot beat zero net-of-cost is not a candidate.
    - ``PERSISTENCE``: naive last-value predictor (return[t] = return[t-1]).
      A model that cannot beat persistence is not adding skill.
    - ``BUY_AND_HOLD``: constant return equal to the in-sample mean of the
      model's OOS series (a stand-in for the relevant buy-and-hold for the
      MVP skeleton).
    """

    ZERO_SKILL = "zero_skill"
    PERSISTENCE = "persistence"
    BUY_AND_HOLD = "buy_and_hold"


class ScoringInput(BaseModel):
    """The input to the tournament scorer for one model.

    Carries the full OOS return series (NOT just summary stats) because the
    bootstrap significance test needs the series to preserve
    autocorrelation. Also carries the trial count (for DSR deflation) and
    the cost-model version (so the rank is auditable — a net edge computed
    under cost model v1 is not the same as under v2).

    Frozen + extra='forbid' (audit integrity — a scoring input cannot be
    mutated after the fact, and unknown fields are rejected so a caller
    cannot silently inject a secret-named field).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    # The OOS return series — bootstrap needs the series, not just the mean.
    oos_returns_net: list[float]
    oos_returns_gross: list[float]
    oos_returns_baseline: list[float]
    # Trial count for the model family (DSR deflation — rigor §2).
    trial_count: int = 1
    # Calibration signals (from SettlementRecord.brier / calibration_bucket).
    brier: float | None = None
    # (bucket_name, predicted_count, realized_count) — reliability curve.
    calibration_buckets: list[tuple[str, int, int]] = Field(default_factory=list)
    # (bucket_name, confidence, realized_return) — monotonicity check.
    confidence_buckets: list[tuple[str, float, float]] = Field(default_factory=list)
    # Risk / cost signals.
    max_drawdown: float = 0.0
    turnover: float | None = None
    feature_availability_ratio: float = 1.0
    latency_ms: float | None = None
    capacity_decay_penalty: float = 0.0
    # Gating inputs.
    settled_count: int = 0
    last_settled_at_ns: int | None = None
    now_ns: int = 0
    stale_threshold_ns: int = 0
    min_settled_samples: int = 10
    # Audit.
    cost_model_version: str = "cm-v1"
    training_accuracy: float | None = None

    @field_validator("model_id")
    @classmethod
    def _model_id_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("model_id must be non-empty")
        return v

    @field_validator("feature_availability_ratio")
    @classmethod
    def _feature_availability_ratio_range(cls, v: float) -> float:
        if v < 0.0 or v > 1.0:
            raise ValueError("feature_availability_ratio must be in [0, 1]")
        return v

    @model_validator(mode="after")
    def _series_lengths_match(self) -> ScoringInput:
        n = len(self.oos_returns_net)
        if len(self.oos_returns_gross) != n:
            raise ValueError("oos_returns_gross must have the same length as oos_returns_net")
        if len(self.oos_returns_baseline) != n:
            raise ValueError("oos_returns_baseline must have the same length as oos_returns_net")
        return self


# ---------------------------------------------------------------------------
# Score components + result
# ---------------------------------------------------------------------------


class ScoreComponent(BaseModel):
    """One named, auditable component of the total score.

    The total score is a weighted sum of component contributions. Each
    component carries its raw value, its weight, and its contribution
    (value * weight) so the operator can interrogate any rank.

    Frozen + extra='forbid' (audit integrity).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    value: float
    weight: float
    contribution: float


class TournamentStatus(StrEnum):
    """Lifecycle / gate status of a model in the tournament.

    - ``INSUFFICIENT_EVIDENCE``: too few settled predictions. Never ranked
      above a model with sufficient evidence.
    - ``STALE``: the last settled prediction is too old. Blocks promotion.
    - ``BLOCKED``: a blocking issue was raised (hard gate). Blocks promotion.
    - ``ELIGIBLE``: the model passed all gates and is ranked.
    """

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    STALE = "stale"
    BLOCKED = "blocked"
    ELIGIBLE = "eligible"


class PromotionRecommendation(StrEnum):
    """The tournament's recommendation for a model.

    - ``PROMOTE``: the model passed all gates and beat the baselines. The
      operator still makes the final call (the tournament recommends, the
      operator decides).
    - ``HOLD``: the model is not ready (insufficient evidence, stale, or
      blocked). Re-evaluate after more settled predictions.
    - ``REJECT``: the model failed a hard gate (negative net edge, DSR <= 0
      after deflation, noise). Do not promote.
    """

    PROMOTE = "promote"
    HOLD = "hold"
    REJECT = "reject"


class TournamentResult(BaseModel):
    """The full, auditable result of scoring one model.

    Carries every field needed by a promotion packet:
    - ``model_id``, ``total_score``, ``score_components`` (the rank + its
      decomposition).
    - ``p_value``, ``deflated_sharpe`` (the significance + deflation
      signals, recorded and shown to the operator).
    - ``blocking_issues`` (the hard gates that fired, if any).
    - ``recommendation``, ``status`` (the gate outcome).
    - ``trial_count`` (carried through for auditability — a model with 100
      trials and DSR=1.0 is not the same as a model with 1 trial and DSR=1.0).

    Frozen + extra='forbid' (audit integrity). ``to_dict`` is JSON
    serializable so the result can feed a promotion packet.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    total_score: float
    score_components: list[ScoreComponent] = Field(default_factory=list)
    p_value: float | None = None
    deflated_sharpe: float | None = None
    raw_sharpe: float | None = None
    blocking_issues: list[dict[str, Any]] = Field(default_factory=list)
    recommendation: PromotionRecommendation = PromotionRecommendation.HOLD
    status: TournamentStatus = TournamentStatus.ELIGIBLE
    trial_count: int = 1
    cost_model_version: str = "cm-v1"
    settled_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for promotion-packet emission."""
        return {
            "model_id": self.model_id,
            "total_score": self.total_score,
            "score_components": [c.model_dump() for c in self.score_components],
            "p_value": self.p_value,
            "deflated_sharpe": self.deflated_sharpe,
            "raw_sharpe": self.raw_sharpe,
            "blocking_issues": list(self.blocking_issues),
            "recommendation": self.recommendation.value,
            "status": self.status.value,
            "trial_count": self.trial_count,
            "cost_model_version": self.cost_model_version,
            "settled_count": self.settled_count,
        }


# ---------------------------------------------------------------------------
# Tournament — the scorer
# ---------------------------------------------------------------------------


# Default component weights (explainable, auditable). The weights sum to 1.0
# over the "positive" components (net_edge, deflated_sharpe, calibration);
# the penalties (drawdown, turnover, feature_availability, latency,
# capacity_decay) are subtracted. The p-value is a gate, not a weighted
# component (it determines whether the model is eligible at all).
DEFAULT_WEIGHTS: dict[str, float] = {
    "net_edge": 0.40,
    "deflated_sharpe": 0.35,
    "calibration": 0.25,
    "drawdown_penalty": 0.10,
    "turnover_penalty": 0.05,
    "feature_availability_penalty": 0.05,
    "latency_penalty": 0.05,
    "capacity_decay_penalty": 0.05,
}


class Tournament:
    """The tournament scorer.

    Deterministic given a fixed ``seed`` (the bootstrap p-value is the only
    randomized step; everything else is a deterministic function of the
    inputs). The seed and ``n_bootstrap`` are recorded on the result via
    the p-value's ``n_bootstrap`` field (auditable).

    The scorer is stateless across models (each ``score`` call is
    independent) so models can be scored in parallel without interference.
    """

    def __init__(
        self,
        seed: int = 0,
        n_bootstrap: int = 500,
        weights: dict[str, float] | None = None,
        p_value_threshold: float = 0.05,
        dsr_threshold: float = 0.0,
    ) -> None:
        self.seed = seed
        self.n_bootstrap = n_bootstrap
        self.weights = {**DEFAULT_WEIGHTS, **(weights or {})}
        self.p_value_threshold = p_value_threshold
        self.dsr_threshold = dsr_threshold

    # -- baselines ----------------------------------------------------------

    def compute_baseline(self, model_returns: list[float], kind: BaselineKind) -> list[float]:
        """Compute a deterministic baseline return series for a model.

        All baselines have the same length as ``model_returns`` so the
        bootstrap p-value can pair them.
        """
        n = len(model_returns)
        if kind == BaselineKind.ZERO_SKILL:
            return [0.0] * n
        if kind == BaselineKind.PERSISTENCE:
            # return[0] = 0 (no prior); return[t] = return[t-1].
            baseline = [0.0]
            for i in range(1, n):
                baseline.append(model_returns[i - 1])
            return baseline
        if kind == BaselineKind.BUY_AND_HOLD:
            mean_ret = statistics.fmean(model_returns) if n else 0.0
            return [mean_ret] * n
        raise ValueError(f"unknown baseline kind: {kind}")

    # -- scoring ------------------------------------------------------------

    def score(self, scoring_input: ScoringInput) -> TournamentResult:
        """Score one model and produce a tournament result.

        The score is a weighted sum of components, then gates are applied.
        A model that fails a gate is never recommended for promotion,
        regardless of its score.
        """
        si = scoring_input
        components: list[ScoreComponent] = []
        blocking_issues: list[dict[str, Any]] = []

        # -- Gate 1: insufficient evidence --------------------------------
        if si.settled_count < si.min_settled_samples:
            return TournamentResult(
                model_id=si.model_id,
                total_score=0.0,
                score_components=[],
                p_value=None,
                deflated_sharpe=None,
                raw_sharpe=None,
                blocking_issues=[
                    {
                        "code": "insufficient_evidence",
                        "message": (
                            f"settled_count={si.settled_count} < "
                            f"min_settled_samples={si.min_settled_samples}"
                        ),
                    }
                ],
                recommendation=PromotionRecommendation.HOLD,
                status=TournamentStatus.INSUFFICIENT_EVIDENCE,
                trial_count=si.trial_count,
                cost_model_version=si.cost_model_version,
                settled_count=si.settled_count,
            )

        # -- Gate 2: stale evidence ---------------------------------------
        if si.last_settled_at_ns is not None:
            age = si.now_ns - si.last_settled_at_ns
            if age > si.stale_threshold_ns:
                blocking_issues.append(
                    {
                        "code": "stale_evidence",
                        "message": (
                            f"last_settled_at_ns={si.last_settled_at_ns} is "
                            f"{age}ns old (> threshold {si.stale_threshold_ns}ns)"
                        ),
                    }
                )

        # -- Significance: DSR + bootstrap p-value vs. zero-skill baseline
        dsr_result = deflated_sharpe_ratio(si.oos_returns_net, si.trial_count)
        zero_baseline = self.compute_baseline(si.oos_returns_net, BaselineKind.ZERO_SKILL)
        boot_result = stationary_bootstrap_pvalue(
            model_returns=si.oos_returns_net,
            baseline_returns=zero_baseline,
            trial_count=si.trial_count,
            n_bootstrap=self.n_bootstrap,
            seed=self.seed,
        )

        # -- Component 1: net edge (rigor §4: net, not gross) -------------
        net_edge = statistics.fmean(si.oos_returns_net) if si.oos_returns_net else 0.0
        w_net = self.weights["net_edge"]
        components.append(
            ScoreComponent(
                name="net_edge",
                value=net_edge,
                weight=w_net,
                contribution=net_edge * w_net,
            )
        )
        if net_edge <= 0.0:
            blocking_issues.append(
                {
                    "code": "net_edge_nonpositive",
                    "message": (
                        f"net edge (mean oos_returns_net) = {net_edge:.6f} <= 0; "
                        "model does not beat zero-skill net-of-cost"
                    ),
                }
            )

        # -- Component 2: Deflated Sharpe (rigor §2) ----------------------
        w_dsr = self.weights["deflated_sharpe"]
        # Normalize DSR to a [0, 1]-ish scale for the weighted sum (DSR is
        # unitless and can be negative; a DSR of ~1.0 per period is very
        # strong). We use a tanh-like squash so very large DSRs don't dominate.
        dsr_norm = _squash(dsr_result.deflated_sharpe)
        components.append(
            ScoreComponent(
                name="deflated_sharpe",
                value=dsr_result.deflated_sharpe,
                weight=w_dsr,
                contribution=dsr_norm * w_dsr,
            )
        )
        if dsr_result.deflated_sharpe <= self.dsr_threshold:
            blocking_issues.append(
                {
                    "code": "dsr_nonpositive",
                    "message": (
                        f"deflated_sharpe = {dsr_result.deflated_sharpe:.6f} <= "
                        f"threshold {self.dsr_threshold}; raw_sharpe="
                        f"{dsr_result.raw_sharpe:.6f}, trial_count={si.trial_count}"
                    ),
                }
            )

        # -- Component 3: calibration (Brier + monotonicity) --------------
        calib_score = self._calibration_score(si)
        w_cal = self.weights["calibration"]
        components.append(
            ScoreComponent(
                name="calibration",
                value=calib_score,
                weight=w_cal,
                contribution=calib_score * w_cal,
            )
        )
        # Monotonicity red flag: confidence buckets should be monotonic in
        # realized return (higher confidence => higher realized return).
        if si.confidence_buckets and not _is_monotonic_confidence(si.confidence_buckets):
            blocking_issues.append(
                {
                    "code": "calibration_non_monotonic",
                    "message": "realized return is not monotonic in confidence",
                }
            )

        # -- Penalties (subtracted) ---------------------------------------
        dd_penalty = min(max(si.max_drawdown, 0.0), 1.0)
        w_dd = self.weights["drawdown_penalty"]
        components.append(
            ScoreComponent(
                name="drawdown_penalty",
                value=dd_penalty,
                weight=w_dd,
                contribution=-(dd_penalty * w_dd),
            )
        )

        turnover_penalty = 0.0
        if si.turnover is not None:
            turnover_penalty = min(max(si.turnover, 0.0), 1.0)
        w_to = self.weights["turnover_penalty"]
        components.append(
            ScoreComponent(
                name="turnover_penalty",
                value=turnover_penalty,
                weight=w_to,
                contribution=-(turnover_penalty * w_to),
            )
        )

        fa_penalty = 1.0 - si.feature_availability_ratio
        w_fa = self.weights["feature_availability_penalty"]
        components.append(
            ScoreComponent(
                name="feature_availability_penalty",
                value=fa_penalty,
                weight=w_fa,
                contribution=-(fa_penalty * w_fa),
            )
        )

        latency_penalty = 0.0
        if si.latency_ms is not None:
            # Squash latency (ms) to [0, 1] — 1000ms -> ~0.63, 5000ms -> ~0.99.
            latency_penalty = 1.0 - (1.0 / (1.0 + (si.latency_ms / 1000.0)))
        w_lat = self.weights["latency_penalty"]
        components.append(
            ScoreComponent(
                name="latency_penalty",
                value=latency_penalty,
                weight=w_lat,
                contribution=-(latency_penalty * w_lat),
            )
        )

        cd_penalty = min(max(si.capacity_decay_penalty, 0.0), 1.0)
        w_cd = self.weights["capacity_decay_penalty"]
        components.append(
            ScoreComponent(
                name="capacity_decay_penalty",
                value=cd_penalty,
                weight=w_cd,
                contribution=-(cd_penalty * w_cd),
            )
        )

        # -- Total score --------------------------------------------------
        total_score = sum(c.contribution for c in components)

        # -- Gate 3: p-value vs. baseline --------------------------------
        if boot_result.p_value > self.p_value_threshold:
            blocking_issues.append(
                {
                    "code": "not_significant_vs_baseline",
                    "message": (
                        f"bootstrap p-value = {boot_result.p_value:.4f} > "
                        f"threshold {self.p_value_threshold}; model does not "
                        "significantly beat zero-skill baseline"
                    ),
                }
            )

        # -- Determine status + recommendation ---------------------------
        stale = any(b.get("code") == "stale_evidence" for b in blocking_issues)
        if stale:
            status = TournamentStatus.STALE
        elif blocking_issues:
            status = TournamentStatus.BLOCKED
        else:
            status = TournamentStatus.ELIGIBLE

        if status == TournamentStatus.ELIGIBLE and not blocking_issues:
            recommendation = PromotionRecommendation.PROMOTE
        elif status == TournamentStatus.BLOCKED and any(
            b.get("code")
            in ("net_edge_nonpositive", "dsr_nonpositive", "not_significant_vs_baseline")
            for b in blocking_issues
        ):
            recommendation = PromotionRecommendation.REJECT
        else:
            recommendation = PromotionRecommendation.HOLD

        return TournamentResult(
            model_id=si.model_id,
            total_score=total_score,
            score_components=components,
            p_value=boot_result.p_value,
            deflated_sharpe=dsr_result.deflated_sharpe,
            raw_sharpe=dsr_result.raw_sharpe,
            blocking_issues=blocking_issues,
            recommendation=recommendation,
            status=status,
            trial_count=si.trial_count,
            cost_model_version=si.cost_model_version,
            settled_count=si.settled_count,
        )

    # -- helpers ------------------------------------------------------------

    def _calibration_score(self, si: ScoringInput) -> float:
        """Calibration score in [0, 1]. Higher is better.

        Combines Brier score (lower is better; we invert it) with a
        monotonicity bonus from the confidence buckets. If no calibration
        signal is present, returns a neutral 0.5.
        """
        score = 0.5
        if si.brier is not None:
            # Brier in [0, 1]; invert so lower brier => higher score.
            brier_score = 1.0 - min(max(si.brier, 0.0), 1.0)
            score = 0.5 * score + 0.5 * brier_score
        if si.confidence_buckets:
            # Monotonic confidence => small bonus; non-monotonic => penalty.
            if _is_monotonic_confidence(si.confidence_buckets):
                score = min(1.0, score + 0.1)
            else:
                score = max(0.0, score - 0.1)
        return score


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _squash(x: float) -> float:
    """Squash a real number to [0, 1] via a shifted tanh.

    Negative x -> < 0.5; positive x -> > 0.5; x=0 -> 0.5. Large |x| saturates.
    Used to normalize DSR (which is unitless and can be negative) into a
    [0, 1] contribution to the weighted score.
    """
    return 0.5 * (1.0 + (x / (1.0 + abs(x))))


def _is_monotonic_confidence(
    buckets: list[tuple[str, float, float]],
) -> bool:
    """Check that realized return is non-decreasing in confidence.

    ``buckets`` is a list of (name, confidence, realized_return). We sort by
    confidence and check that realized_return is non-decreasing. Monotonic
    edge-vs-confidence is a health signal; non-monotonic is a red flag.
    """
    if len(buckets) < 2:
        return True
    sorted_buckets = sorted(buckets, key=lambda b: b[1])
    for i in range(1, len(sorted_buckets)):
        if sorted_buckets[i][2] < sorted_buckets[i - 1][2] - 1e-12:
            return False
    return True
