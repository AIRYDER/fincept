"""Champion/challenger shadow deployment comparison (Tier 2.4).

This module implements the comparison logic between a **champion** model
(the current production or paper-approved model) and a **challenger**
model (a shadow-only model running in parallel). The comparison uses
settled shadow predictions from both models over a fixed evaluation
window to determine whether the challenger should replace the champion.

The comparison is statistical, not heuristic:

  * **Net edge delta**: challenger's mean net OOS return minus champion's.
    Positive = challenger is better.
  * **Deflated Sharpe delta**: challenger's DSR minus champion's.
    Positive = challenger has better risk-adjusted edge.
  * **Bootstrap p-value**: paired bootstrap test on the difference of
    OOS returns. Low p-value = the delta is statistically significant,
    not noise.
  * **Calibration delta**: Brier score difference (lower = better).

The :class:`PromotionDecision` records whether the challenger meets the
promotion threshold (net edge delta > threshold AND p-value < alpha AND
minimum settled count met).

Design notes:

  * Pure-Python, no numpy at module level (imported lazily) so the
    module is importable in lightweight environments.
  * Pydantic v2 models for all results, consistent with the rest of
    ``quant_foundry``.
  * No imports from ``services/`` beyond ``quant_foundry`` itself.
  * The comparison is deterministic given the same inputs (fixed seed
    for the bootstrap).
"""

from __future__ import annotations

import random

from pydantic import BaseModel, ConfigDict, field_validator

from quant_foundry.significance import deflated_sharpe_ratio

__all__ = [
    "ChampionChallengerConfig",
    "ComparisonInput",
    "PromotionDecision",
    "ShadowComparisonResult",
    "compare_champion_challenger",
]


# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #


class ChampionChallengerConfig(BaseModel):
    """Configuration for champion/challenger comparison.

    Args:
        min_settled_count: minimum number of settled predictions
            required for both champion and challenger before a
            comparison is made. Below this, the decision is
            ``"insufficient_evidence"``.
        net_edge_threshold: minimum net edge delta (challenger minus
            champion, in basis points) required for promotion.
            E.g. 50.0 means the challenger must beat the champion by
            at least 50 bps.
        bootstrap_samples: number of bootstrap resamples for the
            paired significance test.
        alpha: significance level for the bootstrap p-value. The
            challenger must have p-value < alpha to be promoted.
        seed: random seed for the bootstrap (deterministic).
        dsr_threshold: minimum DSR for the challenger. If the
            challenger's DSR is below this, promotion is blocked
            even if the net edge delta is positive.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_settled_count: int = 30
    net_edge_threshold: float = 50.0  # basis points
    bootstrap_samples: int = 1000
    alpha: float = 0.05
    seed: int = 42
    dsr_threshold: float = 0.0


# --------------------------------------------------------------------------- #
# Input / Result types                                                        #
# --------------------------------------------------------------------------- #


class ComparisonInput(BaseModel):
    """Settled shadow data for one side of the comparison.

    Args:
        model_id: the model identifier.
        oos_returns_net: per-prediction net OOS returns (after costs).
        trial_count: number of hyperparameter trials for DSR deflation.
        brier: Brier score (calibration quality, lower = better).
        settled_count: number of settled predictions.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    oos_returns_net: list[float]
    trial_count: int = 1
    brier: float | None = None
    settled_count: int = 0

    @field_validator("model_id")
    @classmethod
    def _model_id_nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("model_id must be non-empty")
        return v


class ShadowComparisonResult(BaseModel):
    """Result of a champion/challenger shadow comparison.

    Records the full statistical comparison so an operator (or the
    promotion gate) can audit the decision.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    champion_model_id: str
    challenger_model_id: str
    champion_settled_count: int
    challenger_settled_count: int
    champion_net_edge_bps: float
    challenger_net_edge_bps: float
    net_edge_delta_bps: float
    champion_dsr: float
    challenger_dsr: float
    dsr_delta: float
    bootstrap_p_value: float
    champion_brier: float | None = None
    challenger_brier: float | None = None
    brier_delta: float | None = None
    config: ChampionChallengerConfig


class PromotionDecision(BaseModel):
    """The promotion decision derived from a shadow comparison.

    The ``decision`` field is one of:

      * ``"promote"`` — challenger meets all thresholds, should replace
        champion.
      * ``"insufficient_evidence"`` — not enough settled predictions.
      * ``"no_edge"`` — net edge delta below threshold.
      * ``"not_significant"`` — bootstrap p-value >= alpha.
      * ``"low_dsr"`` — challenger DSR below the DSR threshold.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: str
    result: ShadowComparisonResult
    reason: str


# --------------------------------------------------------------------------- #
# Core comparison logic                                                       #
# --------------------------------------------------------------------------- #


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _bps(returns: list[float]) -> float:
    """Convert mean return to basis points."""
    return _mean(returns) * 10_000.0


def _paired_bootstrap_pvalue(
    champion_returns: list[float],
    challenger_returns: list[float],
    n_samples: int,
    seed: int,
) -> float:
    """Paired bootstrap p-value for H0: challenger_edge <= champion_edge.

    Resamples the paired difference series (challenger - champion) with
    replacement and computes the fraction of resamples where the mean
    difference is <= 0. Low p-value → the challenger's edge is
    significantly higher than the champion's.

    The two series must be the same length (paired). If they differ in
    length, the longer series is truncated to match the shorter (this
    preserves the pairing for the overlapping period).
    """
    n = min(len(champion_returns), len(challenger_returns))
    if n < 2:
        return 1.0

    # Paired differences
    diffs = [challenger_returns[i] - champion_returns[i] for i in range(n)]

    rng = random.Random(seed)
    count_leq_zero = 0
    for _ in range(n_samples):
        # Resample with replacement
        resampled = [diffs[rng.randint(0, n - 1)] for _ in range(n)]
        if _mean(resampled) <= 0.0:
            count_leq_zero += 1

    return count_leq_zero / n_samples


def compare_champion_challenger(
    champion: ComparisonInput,
    challenger: ComparisonInput,
    config: ChampionChallengerConfig,
) -> PromotionDecision:
    """Compare a challenger model against a champion.

    The comparison uses settled shadow predictions from both models.
    The challenger must beat the champion on:

      1. Net edge (by at least ``config.net_edge_threshold`` bps)
      2. Bootstrap significance (p-value < ``config.alpha``)
      3. DSR (challenger DSR >= ``config.dsr_threshold``)

    Args:
        champion: settled shadow data for the current champion.
        challenger: settled shadow data for the challenger.
        config: comparison configuration (thresholds, sample counts).

    Returns:
        A :class:`PromotionDecision` with the full comparison result
        and the decision reason.
    """
    # --- compute metrics for both sides --------------------------------
    champ_dsr_result = deflated_sharpe_ratio(
        champion.oos_returns_net,
        champion.trial_count,
    )
    chal_dsr_result = deflated_sharpe_ratio(
        challenger.oos_returns_net,
        challenger.trial_count,
    )

    champ_net_edge = _bps(champion.oos_returns_net)
    chal_net_edge = _bps(challenger.oos_returns_net)
    net_edge_delta = chal_net_edge - champ_net_edge

    champ_dsr = champ_dsr_result.deflated_sharpe
    chal_dsr = chal_dsr_result.deflated_sharpe
    dsr_delta = chal_dsr - champ_dsr

    # Paired bootstrap p-value
    p_value = _paired_bootstrap_pvalue(
        champion.oos_returns_net,
        challenger.oos_returns_net,
        config.bootstrap_samples,
        config.seed,
    )

    brier_delta = None
    if champion.brier is not None and challenger.brier is not None:
        brier_delta = challenger.brier - champion.brier  # lower = better

    result = ShadowComparisonResult(
        champion_model_id=champion.model_id,
        challenger_model_id=challenger.model_id,
        champion_settled_count=champion.settled_count,
        challenger_settled_count=challenger.settled_count,
        champion_net_edge_bps=champ_net_edge,
        challenger_net_edge_bps=chal_net_edge,
        net_edge_delta_bps=net_edge_delta,
        champion_dsr=champ_dsr,
        challenger_dsr=chal_dsr,
        dsr_delta=dsr_delta,
        bootstrap_p_value=p_value,
        champion_brier=champion.brier,
        challenger_brier=challenger.brier,
        brier_delta=brier_delta,
        config=config,
    )

    # --- decision logic ------------------------------------------------
    min_count = config.min_settled_count
    if champion.settled_count < min_count or challenger.settled_count < min_count:
        return PromotionDecision(
            decision="insufficient_evidence",
            result=result,
            reason=(
                f"need at least {min_count} settled predictions for both "
                f"models; champion has {champion.settled_count}, "
                f"challenger has {challenger.settled_count}"
            ),
        )

    if net_edge_delta < config.net_edge_threshold:
        return PromotionDecision(
            decision="no_edge",
            result=result,
            reason=(
                f"net edge delta {net_edge_delta:.1f} bps < "
                f"threshold {config.net_edge_threshold:.1f} bps"
            ),
        )

    if p_value >= config.alpha:
        return PromotionDecision(
            decision="not_significant",
            result=result,
            reason=(f"bootstrap p-value {p_value:.4f} >= alpha {config.alpha}"),
        )

    if chal_dsr < config.dsr_threshold:
        return PromotionDecision(
            decision="low_dsr",
            result=result,
            reason=(f"challenger DSR {chal_dsr:.4f} < threshold {config.dsr_threshold:.4f}"),
        )

    return PromotionDecision(
        decision="promote",
        result=result,
        reason=(
            f"challenger beats champion by {net_edge_delta:.1f} bps "
            f"(p={p_value:.4f}, DSR delta={dsr_delta:.4f})"
        ),
    )
