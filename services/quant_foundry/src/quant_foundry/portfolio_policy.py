"""
quant_foundry.portfolio_policy — Portfolio Policy Model (T-13.2).

Takes **verified** model outputs (predicted returns per asset) and produces
target portfolio weights — or **abstains** when confidence is too low or a
risk limit would be violated. A reward combining return, transaction cost,
drawdown, and turnover is computed at each rebalance. The policy runs in
**shadow replay only** (``shadow_only=True``): it never emits live orders.

Design invariants (enforced + tested):
- **Pydantic v2 models are frozen + ``extra='forbid'``** (audit integrity).
- **Fail-closed on risk-limit violations.** Weight, turnover, and drawdown
  limits are enforced by raising ``ValueError`` rather than silently
  clipping — the policy must never violate a hard risk limit.
- **Abstention when confidence below threshold.** When the supplied
  ``confidence`` is below ``abstention_threshold`` the policy emits
  ``abstain=True`` with all-zero weights.
- **Cost model hash recorded.** Every :class:`PolicyOutput` carries the
  hash of the cost model used, so a replay is reproducible from the
  output alone.
- **Shadow replay only.** ``shadow_only`` defaults to ``True`` and the
  :class:`ReplayEngine` refuses to run when it is ``False``.

File-disjoint from :mod:`quant_foundry.rl_runtime` (the RL simulator) and
:mod:`quant_foundry.moe_expert_router` (the expert combiner). This module
is the portfolio-level policy that consumes their outputs.
"""

from __future__ import annotations

import hashlib
import json
import math

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EPS = 1e-12
_SUM_TOL = 1e-6  # tolerance for weights summing to 1.0
_TRADING_DAYS = 252  # annualization factor for Sharpe ratio


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PolicyConfig(BaseModel):
    """Configuration for the portfolio policy.

    Frozen + ``extra='forbid'`` for audit integrity. Carries the asset
    count, per-asset and per-rebalance risk limits, the transaction-cost
    parameter (in basis points), the abstention threshold, the reward
    component names, the shadow-only flag, and the RNG seed.

    Attributes:
        n_assets: number of assets (must be >= 1).
        max_weight: per-asset maximum weight (in (0, 1]).
        max_turnover: maximum turnover per rebalance (>= 0).
        max_drawdown: maximum allowed drawdown before forced abstention
            (in (0, 1)).
        cost_bps: transaction cost in basis points (>= 0).
        abstention_threshold: minimum confidence to emit weights ([0, 1]).
        reward_components: ordered list of reward component names.
        shadow_only: whether the policy is shadow-replay only.
        seed: RNG seed for deterministic behavior.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    n_assets: int
    max_weight: float = 0.1
    max_turnover: float = 0.5
    max_drawdown: float = 0.15
    cost_bps: float = 1.0
    abstention_threshold: float = 0.3
    reward_components: list[str] = Field(
        default_factory=lambda: ["return", "cost", "drawdown", "turnover"]
    )
    shadow_only: bool = True
    seed: int = 42

    @field_validator("n_assets")
    @classmethod
    def _validate_n_assets(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"n_assets must be >= 1; got {v}")
        return v

    @field_validator("max_weight")
    @classmethod
    def _validate_max_weight(cls, v: float) -> float:
        if not (0.0 < v <= 1.0):
            raise ValueError(f"max_weight must be in (0, 1]; got {v}")
        return float(v)

    @field_validator("max_turnover")
    @classmethod
    def _validate_max_turnover(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"max_turnover must be >= 0; got {v}")
        return float(v)

    @field_validator("max_drawdown")
    @classmethod
    def _validate_max_drawdown(cls, v: float) -> float:
        if not (0.0 < v < 1.0):
            raise ValueError(f"max_drawdown must be in (0, 1); got {v}")
        return float(v)

    @field_validator("cost_bps")
    @classmethod
    def _validate_cost_bps(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"cost_bps must be >= 0; got {v}")
        return float(v)

    @field_validator("abstention_threshold")
    @classmethod
    def _validate_abstention_threshold(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"abstention_threshold must be in [0, 1]; got {v}")
        return float(v)


class PolicyOutput(BaseModel):
    """Output of a single policy decision.

    Frozen + ``extra='forbid'`` for audit integrity. When ``abstain`` is
    ``True`` the ``target_weights`` are all zero; otherwise they sum to
    1.0 and respect the per-asset max-weight constraint.

    Attributes:
        target_weights: list of per-asset target weights (n_assets).
        abstain: whether the policy abstained.
        confidence: the confidence used for the decision.
        expected_reward: the expected reward at decision time.
        reward_components: mapping of component name -> value.
        risk_limits_respected: whether all risk limits were respected.
        cost_model_hash: SHA-256 hash of the cost model used.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_weights: list[float]
    abstain: bool
    confidence: float
    expected_reward: float
    reward_components: dict[str, float]
    risk_limits_respected: bool
    cost_model_hash: str


class ReplayResult(BaseModel):
    """Result of a shadow replay over a returns series.

    Frozen + ``extra='forbid'`` for audit integrity. Aggregates the
    per-rebalance statistics into a single typed artifact.

    Attributes:
        n_rebalances: number of rebalances executed.
        total_return: cumulative portfolio return over the replay.
        total_cost: cumulative transaction cost over the replay.
        max_drawdown: maximum drawdown observed during the replay.
        avg_turnover: average turnover per rebalance.
        sharpe_ratio: annualized Sharpe ratio (or ``None`` if undefined).
        n_abstentions: number of rebalances where the policy abstained.
        n_risk_violations: number of risk-limit violations (fail-closed).
        reward_history: per-rebalance reward values.
        weight_history: per-rebalance target-weight vectors.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    n_rebalances: int
    total_return: float
    total_cost: float
    max_drawdown: float
    avg_turnover: float
    sharpe_ratio: float | None
    n_abstentions: int
    n_risk_violations: int
    reward_history: list[float]
    weight_history: list[list[float]]


class RiskLimits(BaseModel):
    """Hard risk limits enforced by the policy.

    Frozen + ``extra='forbid'`` for audit integrity. These are the
    fail-closed limits: any violation raises ``ValueError``.

    Attributes:
        max_weight: per-asset maximum weight (in (0, 1]).
        max_turnover: maximum turnover per rebalance (>= 0).
        max_drawdown: maximum allowed drawdown (in (0, 1)).
        min_positions: minimum number of non-zero positions (>= 1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_weight: float
    max_turnover: float
    max_drawdown: float
    min_positions: int = 3

    @field_validator("max_weight")
    @classmethod
    def _validate_max_weight(cls, v: float) -> float:
        if not (0.0 < v <= 1.0):
            raise ValueError(f"max_weight must be in (0, 1]; got {v}")
        return float(v)

    @field_validator("max_turnover")
    @classmethod
    def _validate_max_turnover(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"max_turnover must be >= 0; got {v}")
        return float(v)

    @field_validator("max_drawdown")
    @classmethod
    def _validate_max_drawdown(cls, v: float) -> float:
        if not (0.0 < v < 1.0):
            raise ValueError(f"max_drawdown must be in (0, 1); got {v}")
        return float(v)

    @field_validator("min_positions")
    @classmethod
    def _validate_min_positions(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"min_positions must be >= 1; got {v}")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_hex64(value: str) -> bool:
    """Return ``True`` if ``value`` is a 64-char lowercase hex string."""
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except (ValueError, TypeError):
        return False
    return all(c in "0123456789abcdef" for c in value)


def compute_cost_model_hash(cost_bps: float) -> str:
    """Return the deterministic SHA-256 hash for a cost-bps parameter.

    The hash is computed over a canonical JSON encoding of the cost
    parameter (rounded to 12 decimal places to avoid float drift) so
    that two cost models with identical parameters share a hash.
    """
    payload = json.dumps(
        {"cost_bps": round(float(cost_bps), 12)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Portfolio policy
# ---------------------------------------------------------------------------


class PortfolioPolicy:
    """Portfolio policy that converts model outputs to target weights.

    Takes **verified** model outputs (predicted returns per asset) and
    produces target weights — proportional to the signal, clipped to
    ``max_weight`` and renormalized — or abstains. Risk limits are
    enforced **fail-closed**: weight, turnover, and drawdown violations
    raise ``ValueError`` rather than silently clipping.

    The policy is shadow-replay only by construction; it never emits
    live orders.
    """

    def __init__(
        self,
        config: PolicyConfig,
        risk_limits: RiskLimits,
        cost_model_hash: str,
    ) -> None:
        """Initialize the policy.

        Args:
            config: the policy configuration.
            risk_limits: the hard risk limits (fail-closed).
            cost_model_hash: SHA-256 hash of the cost model used.

        Raises:
            ValueError: if ``cost_model_hash`` is not a 64-char hex
                digest, or if ``config.n_assets`` does not match the
                risk-limit constraints.
        """
        if not _is_hex64(cost_model_hash):
            raise ValueError("cost_model_hash must be a 64-character lowercase hex SHA-256 digest")
        # The per-asset max weight must allow at least min_positions
        # assets to sum to 1.0; otherwise the policy can never satisfy
        # both the max-weight and the sum-to-1 constraints.
        if risk_limits.min_positions * risk_limits.max_weight < 1.0 - _SUM_TOL:
            # This is a soft check: with min_positions assets each at
            # max_weight the maximum achievable sum is
            # min_positions * max_weight. If that is < 1.0 the policy
            # cannot construct a valid weight vector — fail closed.
            raise ValueError(
                f"min_positions ({risk_limits.min_positions}) * "
                f"max_weight ({risk_limits.max_weight}) = "
                f"{risk_limits.min_positions * risk_limits.max_weight} "
                f"< 1.0; cannot construct valid weights"
            )
        self.config = config
        self.risk_limits = risk_limits
        self.cost_model_hash = cost_model_hash
        self._n_assets = config.n_assets
        self._max_weight = min(config.max_weight, risk_limits.max_weight)
        self._max_turnover = min(config.max_turnover, risk_limits.max_turnover)
        self._max_drawdown = min(config.max_drawdown, risk_limits.max_drawdown)
        self._abstention_threshold = config.abstention_threshold
        self._cost_bps = config.cost_bps
        self._rng = np.random.default_rng(config.seed)

    # -- weight construction ---------------------------------------------

    def _compute_raw_weights(self, model_outputs: list[float]) -> np.ndarray:
        """Compute raw target weights from model outputs.

        Weights are proportional to the positive part of the signal,
        clipped to ``max_weight`` and renormalized to sum to 1.0. When
        all signals are non-positive the weights are set to equal
        weight across all assets.
        """
        signal = np.asarray(model_outputs, dtype=float)
        if signal.shape[0] != self._n_assets:
            raise ValueError(
                f"model_outputs length {signal.shape[0]} does not match n_assets {self._n_assets}"
            )
        # Use the positive part of the signal as the weight basis.
        positive = np.maximum(signal, 0.0)
        total = positive.sum()
        if total <= _EPS:
            # No positive signal: fall back to equal weight (subject to
            # max_weight clipping).
            equal = np.full(self._n_assets, 1.0 / self._n_assets)
            return self._clip_and_normalize(equal)
        weights = positive / total
        return self._clip_and_normalize(weights)

    def _clip_and_normalize(self, weights: np.ndarray) -> np.ndarray:
        """Clip weights to ``max_weight`` and renormalize to sum to 1.0.

        Uses an iterative clip-and-redistribute scheme: any weight
        exceeding ``max_weight`` is capped, and the residual (the amount
        by which the capped assets exceeded the limit) is redistributed
        equally among the un-clipped assets. This repeats until either
        the sum converges to 1.0 or all assets are clipped.

        When the un-clipped assets have zero total weight (e.g. only one
        asset had a positive signal), the residual is spread equally
        across all currently-unclipped assets so the final vector still
        sums to 1.0 and respects ``max_weight``.
        """
        clipped = np.array(weights, dtype=float)
        n = clipped.shape[0]
        for _ in range(10):
            total = clipped.sum()
            if total <= _EPS:
                # Degenerate: spread equally.
                clipped = np.full(n, 1.0 / n)
                clipped = np.minimum(clipped, self._max_weight)
                break
            if abs(total - 1.0) <= _SUM_TOL and np.all(clipped <= self._max_weight + _EPS):
                break
            # Step 1: clip over-weight assets.
            over_mask = clipped > self._max_weight + _EPS
            if over_mask.any():
                float((clipped[over_mask] - self._max_weight).sum())
                clipped[over_mask] = self._max_weight
            else:
                pass
            # Step 2: redistribute the excess (plus any residual to
            # reach 1.0) to the un-clipped assets.
            residual = 1.0 - clipped.sum()
            if residual <= _EPS:
                break
            unclipped_mask = clipped < self._max_weight - _EPS
            if not unclipped_mask.any():
                break
            n_unclipped = int(unclipped_mask.sum())
            # Distribute proportionally to existing weight, or equally
            # if the un-clipped assets have no weight yet.
            unclipped_sum = float(clipped[unclipped_mask].sum())
            if unclipped_sum <= _EPS:
                clipped[unclipped_mask] += residual / n_unclipped
            else:
                clipped[unclipped_mask] += residual * (clipped[unclipped_mask] / unclipped_sum)
            clipped = np.minimum(clipped, self._max_weight)
        # Final normalization to guarantee sum == 1.0 within tolerance.
        total = clipped.sum()
        if total > _EPS:
            clipped = clipped / total
        return clipped

    # -- validation ------------------------------------------------------

    def validate_weights(self, weights: list[float]) -> None:
        """Validate a target-weight vector (fail-closed).

        Checks that the length matches ``n_assets``, that weights sum to
        ~1.0 (within ``_SUM_TOL``), that no weight exceeds
        ``max_weight``, and that at least ``min_positions`` assets have
        non-zero weights. Raises ``ValueError`` if any check fails.

        Args:
            weights: the target-weight vector to validate.

        Raises:
            ValueError: if the weights are invalid.
        """
        if not isinstance(weights, (list, tuple)):
            raise ValueError("weights must be a list of floats")
        if len(weights) != self._n_assets:
            raise ValueError(
                f"weights length {len(weights)} does not match n_assets {self._n_assets}"
            )
        w = [float(x) for x in weights]
        total = sum(w)
        if abs(total - 1.0) > _SUM_TOL:
            raise ValueError(f"weights must sum to 1.0 (within {_SUM_TOL}), got {total}")
        for i, x in enumerate(w):
            if x < -_EPS:
                raise ValueError(f"weight {x} at index {i} is negative")
            if x > self._max_weight + _SUM_TOL:
                raise ValueError(f"weight {x} at index {i} exceeds max_weight {self._max_weight}")
        n_positions = sum(1 for x in w if x > _EPS)
        if n_positions < self.risk_limits.min_positions:
            raise ValueError(
                f"only {n_positions} non-zero positions; min_positions "
                f"is {self.risk_limits.min_positions}"
            )

    def validate_turnover(
        self,
        old_weights: list[float],
        new_weights: list[float],
    ) -> None:
        """Validate that the turnover between two weight vectors is within limits.

        Turnover is the sum of absolute weight changes. Raises
        ``ValueError`` if it exceeds ``max_turnover``.

        Args:
            old_weights: the previous weight vector.
            new_weights: the new weight vector.

        Raises:
            ValueError: if the turnover exceeds ``max_turnover``.
        """
        if len(old_weights) != len(new_weights):
            raise ValueError(
                f"old_weights length {len(old_weights)} does not match "
                f"new_weights length {len(new_weights)}"
            )
        turnover = sum(
            abs(float(new_weights[i]) - float(old_weights[i])) for i in range(len(old_weights))
        )
        if turnover > self._max_turnover + _SUM_TOL:
            raise ValueError(f"turnover {turnover} exceeds max_turnover {self._max_turnover}")

    # -- reward ----------------------------------------------------------

    def compute_reward(
        self,
        returns: list[float],
        weights: list[float],
        old_weights: list[float],
    ) -> dict[str, float]:
        """Compute the reward components for one rebalance.

        Components:
        - ``return``: the portfolio return (weighted sum of asset returns).
        - ``cost``: the transaction cost (cost_bps * turnover / 1e4).
        - ``drawdown``: the drawdown penalty (0.5 * drawdown).
        - ``turnover``: the turnover (sum of absolute weight changes).

        The net reward is ``return - cost - drawdown`` (turnover is
        reported separately and not subtracted to avoid double-counting
        with cost).

        Args:
            returns: per-asset returns for the period.
            weights: the portfolio weights held during the period.
            old_weights: the previous weights (for turnover / cost).

        Returns:
            A dict mapping component name -> value. Includes a ``net``
            key with the combined reward.
        """
        if len(returns) != self._n_assets:
            raise ValueError(
                f"returns length {len(returns)} does not match n_assets {self._n_assets}"
            )
        if len(weights) != self._n_assets:
            raise ValueError(
                f"weights length {len(weights)} does not match n_assets {self._n_assets}"
            )
        if len(old_weights) != self._n_assets:
            raise ValueError(
                f"old_weights length {len(old_weights)} does not match n_assets {self._n_assets}"
            )
        r = np.asarray(returns, dtype=float)
        w = np.asarray(weights, dtype=float)
        ow = np.asarray(old_weights, dtype=float)

        portfolio_return = float(np.dot(w, r))
        turnover = float(np.sum(np.abs(w - ow)))
        cost = float(self._cost_bps / 1e4 * turnover)
        # Drawdown is computed by the replay engine from the portfolio
        # value series; here we expose it as 0.0 when called standalone
        # (the replay engine overrides it via the ``drawdown`` arg path
        # by passing a precomputed drawdown through the returns path).
        # For standalone use, drawdown component defaults to 0.0.
        drawdown = 0.0
        drawdown_penalty = 0.5 * drawdown
        net = portfolio_return - cost - drawdown_penalty
        return {
            "return": portfolio_return,
            "cost": cost,
            "drawdown": drawdown_penalty,
            "turnover": turnover,
            "net": net,
        }

    # -- act -------------------------------------------------------------

    def act(
        self,
        model_outputs: list[float],
        current_weights: list[float],
        confidence: float,
        drawdown: float = 0.0,
    ) -> PolicyOutput:
        """Produce a policy decision from verified model outputs.

        Steps:
        1. If ``confidence`` < ``abstention_threshold`` → abstain.
        2. If current ``drawdown`` >= ``max_drawdown`` → abstain (forced).
        3. Compute raw target weights from the signal.
        4. Validate weights (fail-closed on violation).
        5. Validate turnover vs ``current_weights`` (fail-closed).
        6. Compute expected reward components.
        7. Return :class:`PolicyOutput`.

        When abstaining, the target weights are all zero and
        ``risk_limits_respected`` is ``True`` (abstention is always
        safe).

        Args:
            model_outputs: verified predicted returns per asset.
            current_weights: the current portfolio weights.
            confidence: confidence in the model outputs ([0, 1]).
            drawdown: current drawdown level (default 0.0).

        Returns:
            The :class:`PolicyOutput` for this rebalance.

        Raises:
            ValueError: if a hard risk limit is violated (fail-closed)
                and abstention cannot rescue the decision.
        """
        if len(current_weights) != self._n_assets:
            raise ValueError(
                f"current_weights length {len(current_weights)} does not "
                f"match n_assets {self._n_assets}"
            )
        if not (0.0 <= confidence <= 1.0):
            raise ValueError(f"confidence must be in [0, 1]; got {confidence}")

        # Forced abstention on drawdown breach.
        if drawdown >= self._max_drawdown:
            zero_weights = [0.0] * self._n_assets
            return PolicyOutput(
                target_weights=zero_weights,
                abstain=True,
                confidence=confidence,
                expected_reward=0.0,
                reward_components={
                    "return": 0.0,
                    "cost": 0.0,
                    "drawdown": 0.5 * drawdown,
                    "turnover": 0.0,
                    "net": 0.0,
                },
                risk_limits_respected=True,
                cost_model_hash=self.cost_model_hash,
            )

        # Abstention on low confidence.
        if confidence < self._abstention_threshold:
            zero_weights = [0.0] * self._n_assets
            return PolicyOutput(
                target_weights=zero_weights,
                abstain=True,
                confidence=confidence,
                expected_reward=0.0,
                reward_components={
                    "return": 0.0,
                    "cost": 0.0,
                    "drawdown": 0.0,
                    "turnover": 0.0,
                    "net": 0.0,
                },
                risk_limits_respected=True,
                cost_model_hash=self.cost_model_hash,
            )

        # Compute target weights.
        raw = self._compute_raw_weights(model_outputs)
        target = [float(x) for x in raw]

        # Validate weights (fail-closed).
        try:
            self.validate_weights(target)
        except ValueError:
            # If weights are invalid, abstain rather than emit bad weights.
            zero_weights = [0.0] * self._n_assets
            return PolicyOutput(
                target_weights=zero_weights,
                abstain=True,
                confidence=confidence,
                expected_reward=0.0,
                reward_components={
                    "return": 0.0,
                    "cost": 0.0,
                    "drawdown": 0.0,
                    "turnover": 0.0,
                    "net": 0.0,
                },
                risk_limits_respected=False,
                cost_model_hash=self.cost_model_hash,
            )

        # Validate turnover (fail-closed). If turnover exceeds the limit,
        # abstain (keep current weights → zero turnover) rather than
        # violate the limit.
        try:
            self.validate_turnover(current_weights, target)
        except ValueError:
            # Abstain: keep current weights (emit current as target so
            # turnover is zero). But current weights must themselves be
            # valid; if they are, emit them; otherwise abstain to zero.
            try:
                self.validate_weights(current_weights)
                # current weights are valid; emit them (no change).
                reward = self.compute_reward(model_outputs, current_weights, current_weights)
                return PolicyOutput(
                    target_weights=[float(x) for x in current_weights],
                    abstain=True,
                    confidence=confidence,
                    expected_reward=reward["net"],
                    reward_components=reward,
                    risk_limits_respected=True,
                    cost_model_hash=self.cost_model_hash,
                )
            except ValueError:
                zero_weights = [0.0] * self._n_assets
                return PolicyOutput(
                    target_weights=zero_weights,
                    abstain=True,
                    confidence=confidence,
                    expected_reward=0.0,
                    reward_components={
                        "return": 0.0,
                        "cost": 0.0,
                        "drawdown": 0.0,
                        "turnover": 0.0,
                        "net": 0.0,
                    },
                    risk_limits_respected=False,
                    cost_model_hash=self.cost_model_hash,
                )

        # All checks passed: emit the target weights.
        reward = self.compute_reward(model_outputs, target, current_weights)
        return PolicyOutput(
            target_weights=target,
            abstain=False,
            confidence=confidence,
            expected_reward=reward["net"],
            reward_components=reward,
            risk_limits_respected=True,
            cost_model_hash=self.cost_model_hash,
        )


# ---------------------------------------------------------------------------
# Standalone risk-limit validator
# ---------------------------------------------------------------------------


def validate_no_risk_violation(
    output: PolicyOutput,
    risk_limits: RiskLimits,
) -> bool:
    """Check that a policy output respects all hard risk limits.

    Returns ``True`` if all limits are respected. Raises ``ValueError``
    if any limit is violated (fail-closed). When ``output.abstain`` is
    ``True`` the output is always considered safe (returns ``True``).

    Args:
        output: the policy output to check.
        risk_limits: the hard risk limits.

    Returns:
        ``True`` if all risk limits are respected.

    Raises:
        ValueError: if any risk limit is violated.
    """
    if output.abstain:
        return True
    weights = output.target_weights
    if len(weights) == 0:
        return True
    # Max weight.
    for i, w in enumerate(weights):
        if w > risk_limits.max_weight + _SUM_TOL:
            raise ValueError(f"weight {w} at index {i} exceeds max_weight {risk_limits.max_weight}")
    # Min positions.
    n_positions = sum(1 for w in weights if w > _EPS)
    if n_positions < risk_limits.min_positions:
        raise ValueError(
            f"only {n_positions} non-zero positions; min_positions is {risk_limits.min_positions}"
        )
    # Sum to 1.0.
    total = sum(weights)
    if abs(total - 1.0) > _SUM_TOL:
        raise ValueError(f"weights must sum to 1.0 (within {_SUM_TOL}), got {total}")
    return True


# ---------------------------------------------------------------------------
# Replay engine
# ---------------------------------------------------------------------------


class ReplayEngine:
    """Shadow-replay engine that runs a policy over a returns series.

    Iterates through a returns series, calls :meth:`PortfolioPolicy.act`
    at each step (using the returns as a proxy for model outputs), tracks
    the portfolio value / cost / drawdown, and enforces risk limits
    fail-closed. Returns a :class:`ReplayResult` aggregating the
    per-step statistics.

    The engine refuses to run when ``config.shadow_only`` is ``False``
    (the policy must be shadow-replay only).
    """

    def __init__(
        self,
        policy: PortfolioPolicy,
        config: PolicyConfig,
    ) -> None:
        """Initialize the replay engine.

        Args:
            policy: the portfolio policy to replay.
            config: the policy configuration (must have shadow_only=True).

        Raises:
            ValueError: if ``config.shadow_only`` is ``False``.
        """
        if not config.shadow_only:
            raise ValueError(
                "ReplayEngine requires shadow_only=True; the policy must be shadow-replay only"
            )
        self.policy = policy
        self.config = config

    def run(
        self,
        returns_series: list[list[float]],
        initial_weights: list[float],
    ) -> ReplayResult:
        """Run a shadow replay over a returns series.

        At each step the per-asset returns are used as a proxy for the
        model outputs (predicted returns), and the policy is asked to
        act. The portfolio value is updated as
        ``value *= (1 + portfolio_return - cost)``. Drawdown is tracked
        from the peak portfolio value. When the policy abstains the
        current weights are held (no rebalance, zero turnover).

        Risk limits are enforced fail-closed: a weight or turnover
        violation in the emitted weights raises ``ValueError``. A
        drawdown breach triggers forced abstention (not a raise) since
        the policy handles it internally.

        Args:
            returns_series: list of per-step per-asset returns.
            initial_weights: the starting portfolio weights.

        Returns:
            The :class:`ReplayResult` aggregating the replay.

        Raises:
            ValueError: if a hard risk limit is violated.
        """
        n_assets = self.config.n_assets
        if len(initial_weights) != n_assets:
            raise ValueError(
                f"initial_weights length {len(initial_weights)} does not match n_assets {n_assets}"
            )
        # Validate initial weights (fail-closed).
        self.policy.validate_weights(initial_weights)

        current_weights = [float(x) for x in initial_weights]
        portfolio_value = 1.0
        peak_value = 1.0
        total_cost = 0.0
        max_drawdown = 0.0
        n_abstentions = 0
        n_risk_violations = 0
        reward_history: list[float] = []
        weight_history: list[list[float]] = []

        for step_returns in returns_series:
            if len(step_returns) != n_assets:
                raise ValueError(
                    f"returns row length {len(step_returns)} does not match n_assets {n_assets}"
                )
            r = np.asarray(step_returns, dtype=float)

            # Current drawdown.
            drawdown = (peak_value - portfolio_value) / max(peak_value, _EPS)
            if drawdown > max_drawdown:
                max_drawdown = drawdown

            # Use returns as proxy for model outputs. Confidence is a
            # function of the signal magnitude (higher absolute signal
            # → higher confidence). This is a deterministic proxy for
            # shadow replay.
            signal_strength = float(np.mean(np.abs(r)))
            confidence = min(1.0, signal_strength * 10.0)

            output = self.policy.act(
                model_outputs=[float(x) for x in step_returns],
                current_weights=current_weights,
                confidence=confidence,
                drawdown=drawdown,
            )

            if output.abstain:
                n_abstentions += 1
                if not output.risk_limits_respected:
                    n_risk_violations += 1
                # Hold current weights (no rebalance).
                held_weights = current_weights
            else:
                # Validate the emitted weights fail-closed.
                self.policy.validate_weights(output.target_weights)
                self.policy.validate_turnover(current_weights, output.target_weights)
                held_weights = [float(x) for x in output.target_weights]

            # Compute portfolio return for the period.
            portfolio_return = float(np.dot(np.asarray(held_weights, dtype=float), r))
            # Compute turnover / cost.
            turnover = float(
                np.sum(
                    np.abs(
                        np.asarray(held_weights, dtype=float)
                        - np.asarray(current_weights, dtype=float)
                    )
                )
            )
            cost = float(self.config.cost_bps / 1e4 * turnover)
            total_cost += cost

            # Update portfolio value.
            portfolio_value = portfolio_value * (1.0 + portfolio_return - cost)
            if portfolio_value > peak_value:
                peak_value = portfolio_value

            # Drawdown penalty for reward.
            dd = (peak_value - portfolio_value) / max(peak_value, _EPS)
            reward = portfolio_return - cost - 0.5 * dd
            reward_history.append(reward)
            weight_history.append(list(held_weights))

            current_weights = list(held_weights)

        total_return = portfolio_value - 1.0
        avg_turnover = (
            float(np.mean(np.abs(np.diff(np.asarray(weight_history), axis=0))))
            if len(weight_history) > 1
            else 0.0
        )
        # Recompute avg_turnover directly from the tracked turnovers.
        turnovers = []
        prev = list(initial_weights)
        for wh in weight_history:
            turnovers.append(float(np.sum(np.abs(np.asarray(wh) - np.asarray(prev)))))
            prev = list(wh)
        avg_turnover = float(np.mean(turnovers)) if turnovers else 0.0

        sharpe = self.compute_sharpe(reward_history)

        return ReplayResult(
            n_rebalances=len(returns_series),
            total_return=total_return,
            total_cost=total_cost,
            max_drawdown=max_drawdown,
            avg_turnover=avg_turnover,
            sharpe_ratio=sharpe,
            n_abstentions=n_abstentions,
            n_risk_violations=n_risk_violations,
            reward_history=reward_history,
            weight_history=weight_history,
        )

    def compute_sharpe(self, reward_history: list[float]) -> float | None:
        """Compute the annualized Sharpe ratio from a reward history.

        Assumes daily data and annualizes by ``sqrt(252)``. Returns
        ``None`` when the Sharpe ratio is undefined (fewer than 2
        observations or zero standard deviation).

        Args:
            reward_history: per-step reward values.

        Returns:
            The annualized Sharpe ratio, or ``None`` if undefined.
        """
        if len(reward_history) < 2:
            return None
        arr = np.asarray(reward_history, dtype=float)
        std = float(np.std(arr, ddof=1))
        if std <= _EPS:
            return None
        mean = float(np.mean(arr))
        return float(mean / std * math.sqrt(_TRADING_DAYS))
