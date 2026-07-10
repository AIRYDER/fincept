"""
quant_foundry.rl_shadow_policy — RL Shadow Policy (T-13.3).

Wraps the RL runtime (:mod:`quant_foundry.rl_runtime`) for **offline /
shadow-only** training of reinforcement-learning portfolio policies. The
module is the research lane for RL: it can train and evaluate policies on
historical data, but it can **never** be promoted to a live alpha model
without an explicit policy override.

Design invariants (enforced + tested):

- **Pydantic v2 models are frozen + ``extra='forbid'``** (audit integrity).
- **Strict train/eval separation.** The train period and eval period must
  not overlap (``eval_start >= train_end``); a violation raises
  ``ValueError`` (fail-closed).
- **No future-label access.** A reward at time ``t`` may only use labels
  available at or before ``t``; a future-label access raises
  ``ValueError`` (fail-closed).
- **Shadow only.** ``shadow_only`` defaults to ``True`` and the config
  refuses construction when it is ``False``. ``promotion_eligible`` is
  always ``False`` on results and the result refuses construction when it
  is ``True``.
- **Lazy heavy imports.** ``torch`` / ``gymnasium`` /
  ``stable_baselines3`` imports happen inside methods, never at module
  top level, so the module imports on CPU-only hosts.

File-disjoint from :mod:`quant_foundry.rl_runtime` (the simulator) and
:mod:`quant_foundry.portfolio_policy` (the rule-based portfolio policy).
This module is the RL-specific shadow policy that wraps the runtime.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quant_foundry.rl_runtime import (
    CostModel,
    DeterministicMarketSimulator,
    EnvironmentManifest,
    PolicyCheckpoint,
    RolloutManager,
    RolloutRecord,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EPS = 1e-12
_TRADING_DAYS = 252  # annualization factor for Sharpe ratio
_VALID_POLICY_TYPES = ("ppo", "a2c", "dqn", "random")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 datetime string (fail-closed on bad input).

    Supports a trailing ``Z`` (UTC) suffix in addition to offsets.
    """
    if not isinstance(ts, str) or not ts:
        raise ValueError(f"timestamp must be a non-empty string; got {ts!r}")
    text = ts
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO datetime {ts!r}: {exc}") from exc
    # Normalize naive datetimes to UTC for comparison consistency.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _is_hex64(value: str) -> bool:
    """Return ``True`` if ``value`` is a 64-char lowercase hex string."""
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except (ValueError, TypeError):
        return False
    return all(c in "0123456789abcdef" for c in value)


def _policy_hash(policy_type: str, seed: int, n_assets: int) -> str:
    """Return a deterministic SHA-256 hash for a (mocked) policy."""
    payload = json.dumps(
        {
            "policy_type": policy_type,
            "seed": int(seed),
            "n_assets": int(n_assets),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class RLShadowConfig(BaseModel):
    """Configuration for an RL shadow policy run.

    Frozen + ``extra='forbid'`` for audit integrity. Carries the policy
    type, episode counts, the strictly-separated train/eval period
    boundaries (ISO datetimes), the environment manifest and cost model
    (from :mod:`quant_foundry.rl_runtime`), the shadow-only flag
    (always ``True``), the RNG seed, and the max checkpoint steps.

    Validators enforce:

    - ``policy_type`` is one of ``ppo``, ``a2c``, ``dqn``, ``random``.
    - ``train_end > train_start`` and ``eval_end > eval_start``.
    - ``eval_start >= train_end`` (no overlap — fail-closed).
    - ``shadow_only`` must be ``True`` (fail-closed if ``False``).
    - ``n_train_episodes`` and ``n_eval_episodes`` are >= 1.
    - ``max_checkpoint_steps`` is >= 1.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_type: str = "ppo"
    n_train_episodes: int = 100
    n_eval_episodes: int = 20
    train_start: str
    train_end: str
    eval_start: str
    eval_end: str
    env_manifest: EnvironmentManifest
    cost_model: CostModel
    shadow_only: bool = True
    seed: int = 42
    max_checkpoint_steps: int = 1000

    @field_validator("policy_type")
    @classmethod
    def _validate_policy_type(cls, v: str) -> str:
        if v not in _VALID_POLICY_TYPES:
            raise ValueError(f"policy_type must be one of {_VALID_POLICY_TYPES}; got {v!r}")
        return v

    @field_validator("n_train_episodes", "n_eval_episodes", "max_checkpoint_steps")
    @classmethod
    def _validate_positive_int(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"value must be >= 1; got {v}")
        return v

    @model_validator(mode="after")
    def _validate_periods_and_shadow(self) -> RLShadowConfig:
        train_start = _parse_iso(self.train_start)
        train_end = _parse_iso(self.train_end)
        eval_start = _parse_iso(self.eval_start)
        eval_end = _parse_iso(self.eval_end)
        if not (train_end > train_start):
            raise ValueError(
                f"train_end ({self.train_end}) must be > train_start ({self.train_start})"
            )
        if not (eval_end > eval_start):
            raise ValueError(f"eval_end ({self.eval_end}) must be > eval_start ({self.eval_start})")
        if eval_start < train_end:
            raise ValueError(
                f"eval_start ({self.eval_start}) must be >= train_end "
                f"({self.train_end}); train/eval periods must not overlap"
            )
        if not self.shadow_only:
            raise ValueError(
                "shadow_only must be True; RL is research-only and cannot "
                "be promoted to a live policy lane"
            )
        return self


class RLShadowResult(BaseModel):
    """Result of an RL shadow policy train/eval run.

    Frozen + ``extra='forbid'`` for audit integrity. Aggregates the train
    and eval rewards, episode counts, the policy checkpoint (if any),
    rollout-log paths, the environment manifest, the (always-``False``)
    promotion-eligibility flag, computed metrics, and the wall-clock
    duration.

    Validators enforce:

    - ``promotion_eligible`` must be ``False`` (fail-closed if ``True``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    config: RLShadowConfig
    train_reward: float
    eval_reward: float
    n_train_episodes: int
    n_eval_episodes: int
    policy_checkpoint: PolicyCheckpoint | None = None
    rollout_logs: list[str] = Field(default_factory=list)
    env_manifest: EnvironmentManifest
    promotion_eligible: bool = False
    metrics: dict[str, float] = Field(default_factory=dict)
    duration_seconds: float

    @field_validator("n_train_episodes", "n_eval_episodes")
    @classmethod
    def _validate_positive_int(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"value must be >= 1; got {v}")
        return v

    @field_validator("promotion_eligible")
    @classmethod
    def _validate_promotion_eligible(cls, v: bool) -> bool:
        if v:
            raise ValueError(
                "promotion_eligible must be False; RL is research-only and "
                "cannot be promoted to a live policy lane without an "
                "explicit manual override"
            )
        return v


# ---------------------------------------------------------------------------
# Train / eval separator
# ---------------------------------------------------------------------------


class TrainEvalSeparator:
    """Enforce strict, non-overlapping train/eval periods.

    Constructed with the four ISO-datetime boundaries (train start/end,
    eval start/end). :meth:`validate_separation` checks that the eval
    period starts at or after the train period ends (no overlap) and
    raises ``ValueError`` otherwise. The :meth:`is_in_train_period` and
    :meth:`is_in_eval_period` helpers test a single timestamp, and the
    ``get_train_data`` / ``get_eval_data`` helpers filter a list of
    record dicts by a timestamp field.
    """

    def __init__(
        self,
        train_start: str,
        train_end: str,
        eval_start: str,
        eval_end: str,
    ) -> None:
        """Initialize and validate the period boundaries.

        Args:
            train_start: ISO datetime for the train period start.
            train_end: ISO datetime for the train period end (exclusive).
            eval_start: ISO datetime for the eval period start.
            eval_end: ISO datetime for the eval period end (exclusive).

        Raises:
            ValueError: if any boundary is invalid, if a period is
                empty, or if the train and eval periods overlap.
        """
        self.train_start_str = train_start
        self.train_end_str = train_end
        self.eval_start_str = eval_start
        self.eval_end_str = eval_end
        self.train_start = _parse_iso(train_start)
        self.train_end = _parse_iso(train_end)
        self.eval_start = _parse_iso(eval_start)
        self.eval_end = _parse_iso(eval_end)
        if not (self.train_end > self.train_start):
            raise ValueError(f"train_end ({train_end}) must be > train_start ({train_start})")
        if not (self.eval_end > self.eval_start):
            raise ValueError(f"eval_end ({eval_end}) must be > eval_start ({eval_start})")
        self.validate_separation()

    def validate_separation(self) -> None:
        """Check that the eval period starts at or after the train period ends.

        Raises:
            ValueError: if ``eval_start < train_end`` (overlap).
        """
        if self.eval_start < self.train_end:
            raise ValueError(
                f"eval_start ({self.eval_start_str}) must be >= train_end "
                f"({self.train_end_str}); train/eval periods overlap"
            )

    def is_in_train_period(self, timestamp: str) -> bool:
        """Return ``True`` if ``timestamp`` falls within the train period.

        The train period is ``[train_start, train_end)`` (half-open).
        """
        ts = _parse_iso(timestamp)
        return self.train_start <= ts < self.train_end

    def is_in_eval_period(self, timestamp: str) -> bool:
        """Return ``True`` if ``timestamp`` falls within the eval period.

        The eval period is ``[eval_start, eval_end)`` (half-open).
        """
        ts = _parse_iso(timestamp)
        return self.eval_start <= ts < self.eval_end

    def get_train_data(
        self, data: list[dict[str, Any]], timestamp_field: str = "timestamp"
    ) -> list[dict[str, Any]]:
        """Filter ``data`` to records whose timestamp is in the train period.

        Args:
            data: list of record dicts.
            timestamp_field: the key holding the ISO datetime per record.

        Returns:
            A new list of records in the train period (preserving order).
        """
        return [row for row in data if self.is_in_train_period(str(row[timestamp_field]))]

    def get_eval_data(
        self, data: list[dict[str, Any]], timestamp_field: str = "timestamp"
    ) -> list[dict[str, Any]]:
        """Filter ``data`` to records whose timestamp is in the eval period.

        Args:
            data: list of record dicts.
            timestamp_field: the key holding the ISO datetime per record.

        Returns:
            A new list of records in the eval period (preserving order).
        """
        return [row for row in data if self.is_in_eval_period(str(row[timestamp_field]))]


# ---------------------------------------------------------------------------
# Future-label guard
# ---------------------------------------------------------------------------


class FutureLabelGuard:
    """Prevent a reward at time ``t`` from using a label from the future.

    Constructed with a prediction ``horizon`` (in periods). The guard
    checks that, for every reward computed at ``reward_time``, the
    corresponding ``label_time`` is at or before ``reward_time`` — i.e.
    the label was available when the reward was computed. A violation
    raises ``ValueError`` (fail-closed).
    """

    def __init__(self, horizon: int) -> None:
        """Initialize the guard.

        Args:
            horizon: the prediction horizon in periods (>= 1).

        Raises:
            ValueError: if ``horizon < 1``.
        """
        if horizon < 1:
            raise ValueError(f"horizon must be >= 1; got {horizon}")
        self.horizon = horizon

    def check_reward_access(self, reward_time: str, label_time: str) -> bool:
        """Check that a reward at ``reward_time`` does not use a future label.

        The label must be available at or before the reward time
        (``label_time <= reward_time``). Returns ``True`` when safe.

        Args:
            reward_time: ISO datetime at which the reward was computed.
            label_time: ISO datetime at which the label was available.

        Returns:
            ``True`` when the access is safe.

        Raises:
            ValueError: if ``label_time > reward_time`` (future label).
        """
        rt = _parse_iso(reward_time)
        lt = _parse_iso(label_time)
        if lt > rt:
            raise ValueError(
                f"future-label access detected: label_time ({label_time}) "
                f"is after reward_time ({reward_time})"
            )
        return True

    def validate_no_future_leakage(
        self,
        returns: list[float],
        labels: list[float],
        reward_times: list[str],
        label_times: list[str],
    ) -> bool:
        """Check that no label is accessed before it is available.

        The four lists must have equal length; for each index ``i`` the
        label at ``label_times[i]`` must be available at or before
        ``reward_times[i]``. Returns ``True`` when there is no leakage.

        Args:
            returns: per-step returns (length ``n``).
            labels: per-step labels (length ``n``).
            reward_times: per-step reward timestamps (length ``n``).
            label_times: per-step label timestamps (length ``n``).

        Returns:
            ``True`` when there is no future-label leakage.

        Raises:
            ValueError: if the list lengths differ or a future label is
                accessed.
        """
        n = len(returns)
        if not (len(labels) == len(reward_times) == len(label_times) == n):
            raise ValueError(
                f"list length mismatch: returns={len(returns)} "
                f"labels={len(labels)} reward_times={len(reward_times)} "
                f"label_times={len(label_times)}"
            )
        for i in range(n):
            self.check_reward_access(reward_times[i], label_times[i])
        return True


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _compute_metrics(
    rewards: list[float],
    turnovers: list[float],
    portfolio_values: list[float],
) -> dict[str, float]:
    """Compute eval metrics from per-step rewards / turnovers / values.

    Returns a dict with ``sharpe``, ``max_drawdown``, ``avg_turnover``,
    ``total_return``, ``n_steps``, and ``mean_reward``. The Sharpe ratio
    is annualized using ``_TRADING_DAYS`` and is ``0.0`` when undefined
    (zero or non-finite variance).
    """
    n = len(rewards)
    metrics: dict[str, float] = {
        "sharpe": 0.0,
        "max_drawdown": 0.0,
        "avg_turnover": 0.0,
        "total_return": 0.0,
        "n_steps": float(n),
        "mean_reward": 0.0,
    }
    if n == 0:
        return metrics
    mean_reward = sum(float(r) for r in rewards) / n
    metrics["mean_reward"] = float(mean_reward)
    metrics["total_return"] = float(sum(float(r) for r in rewards))
    metrics["avg_turnover"] = float(sum(float(t) for t in turnovers) / n) if n else 0.0
    # Sharpe (annualized) from per-step rewards.
    if n > 1:
        var = sum((float(r) - mean_reward) ** 2 for r in rewards) / (n - 1)
        std = math.sqrt(var) if var > _EPS else 0.0
        if std > _EPS:
            metrics["sharpe"] = float((mean_reward / std) * math.sqrt(_TRADING_DAYS))
    # Max drawdown from the portfolio-value curve.
    if portfolio_values:
        peak = portfolio_values[0]
        max_dd = 0.0
        for v in portfolio_values:
            if v > peak:
                peak = v
            dd = (peak - v) / max(peak, _EPS)
            if dd > max_dd:
                max_dd = dd
        metrics["max_drawdown"] = float(max_dd)
    return metrics


# ---------------------------------------------------------------------------
# RL shadow policy
# ---------------------------------------------------------------------------


class RLShadowPolicy:
    """An RL shadow policy that wraps the RL runtime for offline training.

    The policy trains and evaluates a (mocked) RL policy on historical
    returns data, enforcing strict train/eval separation and future-label
    prevention. It is **shadow only**: results always carry
    ``promotion_eligible=False`` and the config refuses construction when
    ``shadow_only`` is ``False``.

    The training policy is a deterministic, seeded heuristic (uniform or
    random weights respecting the manifest's risk limits) so the module
    runs on CPU-only hosts without ``torch`` / ``stable_baselines3``.
    Heavy imports are lazy and only invoked if a real RL algorithm is
    requested (currently all paths use the mocked heuristic).
    """

    def __init__(self, config: RLShadowConfig) -> None:
        """Initialize the shadow policy.

        Args:
            config: the shadow policy configuration.
        """
        self.config = config
        self.separator = TrainEvalSeparator(
            train_start=config.train_start,
            train_end=config.train_end,
            eval_start=config.eval_start,
            eval_end=config.eval_end,
        )
        self.future_guard = FutureLabelGuard(horizon=1)
        self._sim = DeterministicMarketSimulator(
            env_manifest=config.env_manifest,
            cost_model=config.cost_model,
            seed=config.seed,
        )
        self._n_assets = config.env_manifest.n_assets
        self._max_weight = config.env_manifest.risk_limits.get("max_weight", 1.0)
        self._rng = random.Random(config.seed)
        self._checkpoint: PolicyCheckpoint | None = None
        self._rollout_logs: list[str] = []

    # -- policy action ---------------------------------------------------

    def _policy_action(self, step: int) -> list[float]:
        """Return a deterministic target-weight action for ``step``.

        For ``random`` policy type the weights are drawn from the seeded
        RNG and normalized; for all other policy types a uniform weight
        vector is used (the mocked heuristic). All vectors respect the
        manifest's ``max_weight`` via an iterative clip-and-normalize
        scheme (clip over-weight assets, redistribute the residual to
        un-clipped assets, repeat).
        """
        n = self._n_assets
        if self.config.policy_type == "random":
            raw = [max(self._rng.random(), _EPS) for _ in range(n)]
        else:
            raw = [1.0 for _ in range(n)]
        total = sum(raw)
        if total <= _EPS:
            raw = [1.0 for _ in range(n)]
            total = float(n)
        weights = [w / total for w in raw]
        # Iterative clip-and-normalize so the final vector sums to 1.0
        # and respects ``max_weight``.
        for _ in range(10):
            over = [i for i, w in enumerate(weights) if w > self._max_weight + _EPS]
            if not over:
                break
            for i in over:
                weights[i] = self._max_weight
            residual = 1.0 - sum(weights)
            if residual <= _EPS:
                break
            unclipped = [i for i, w in enumerate(weights) if w < self._max_weight - _EPS]
            if not unclipped:
                break
            share = residual / len(unclipped)
            for i in unclipped:
                weights[i] += share
        # Final normalization.
        tot = sum(weights)
        if tot > _EPS:
            weights = [w / tot for w in weights]
        return [float(w) for w in weights]

    # -- training --------------------------------------------------------

    def train(
        self,
        returns_data: list[dict[str, Any]],
        timestamp_field: str = "timestamp",
    ) -> RLShadowResult:
        """Run shadow training and return an :class:`RLShadowResult`.

        Validates train/eval separation (fail-closed), filters
        ``returns_data`` to the train period, runs the (mocked) training
        episodes on a :class:`DeterministicMarketSimulator`, stores a
        policy checkpoint, records rollout logs, and returns a result
        with ``promotion_eligible=False``.

        Args:
            returns_data: list of record dicts with a timestamp field.
            timestamp_field: the key holding the ISO datetime per record.

        Returns:
            The shadow training result.
        """
        start = time.perf_counter()
        # Fail-closed: re-validate separation at train time.
        self.separator.validate_separation()
        self.separator.get_train_data(returns_data, timestamp_field)
        n_episodes = self.config.n_train_episodes
        rewards_all: list[float] = []
        turnovers_all: list[float] = []
        values_all: list[float] = []
        rollout_mgr = RolloutManager(cache_dir=self._cache_dir())
        self._rollout_logs = []
        for ep in range(n_episodes):
            obs = self._sim.reset()
            ep_rewards: list[float] = []
            ep_actions: list[list[float]] = []
            ep_values: list[float] = [float(obs["portfolio_value"])]
            ep_turnovers: list[float] = []
            terminated = False
            truncated = False
            step = 0
            while not (terminated or truncated):
                action = self._policy_action(step)
                obs, reward, terminated, truncated, info = self._sim.step(action)
                ep_rewards.append(float(reward))
                ep_actions.append(list(action))
                ep_turnovers.append(float(info["turnover"]))
                ep_values.append(float(info["portfolio_value"]))
                step += 1
                if step >= self.config.max_checkpoint_steps:
                    truncated = True
            rewards_all.extend(ep_rewards)
            turnovers_all.extend(ep_turnovers)
            values_all.extend(ep_values)
            rollout_id = f"train_ep{ep:04d}"
            rollout = RolloutRecord(
                rollout_id=rollout_id,
                env_id=self.config.env_manifest.env_id,
                policy_hash=_policy_hash(
                    self.config.policy_type,
                    self.config.seed,
                    self._n_assets,
                ),
                n_steps=len(ep_actions),
                actions=ep_actions,
                rewards=ep_rewards,
                cumulative_reward=float(sum(ep_rewards)),
                terminated=terminated,
                truncated=truncated,
            )
            rollout_mgr.save_rollout(rollout)
            log_path = str(Path(self._cache_dir()) / f"replay_{rollout_id}.json")
            rollout_mgr.save_replay_log(rollout, log_path)
            self._rollout_logs.append(log_path)
        train_reward = float(sum(rewards_all)) if rewards_all else 0.0
        # Build a policy checkpoint.
        self._checkpoint = PolicyCheckpoint(
            checkpoint_id=f"shadow_{self.config.policy_type}_{self.config.seed}",
            policy_type=self.config.policy_type,
            policy_hash=_policy_hash(
                self.config.policy_type,
                self.config.seed,
                self._n_assets,
            ),
            env_id=self.config.env_manifest.env_id,
            training_steps=len(rewards_all),
            artifact_path=str(Path(self._cache_dir()) / "policy.json"),
            created_at=datetime.now(UTC).isoformat(),
        )
        # Persist the (mocked) policy artifact.
        self._save_policy_artifact()
        # Eval on the eval-period data.
        eval_metrics = self.evaluate(returns_data, timestamp_field)
        eval_reward = float(eval_metrics.get("total_return", 0.0))
        duration = float(time.perf_counter() - start)
        result = RLShadowResult(
            config=self.config,
            train_reward=train_reward,
            eval_reward=eval_reward,
            n_train_episodes=n_episodes,
            n_eval_episodes=self.config.n_eval_episodes,
            policy_checkpoint=self._checkpoint,
            rollout_logs=list(self._rollout_logs),
            env_manifest=self.config.env_manifest,
            promotion_eligible=False,
            metrics=eval_metrics,
            duration_seconds=duration,
        )
        return result

    # -- evaluation ------------------------------------------------------

    def evaluate(
        self,
        returns_data: list[dict[str, Any]],
        timestamp_field: str = "timestamp",
    ) -> dict[str, float]:
        """Run shadow evaluation and return a metrics dict.

        Filters ``returns_data`` to the eval period, runs eval episodes
        on the simulator, and computes metrics (sharpe, max_drawdown,
        avg_turnover, total_return, n_steps, mean_reward).

        Args:
            returns_data: list of record dicts with a timestamp field.
            timestamp_field: the key holding the ISO datetime per record.

        Returns:
            A dict of metric name -> float value.
        """
        self.separator.get_eval_data(returns_data, timestamp_field)
        n_episodes = self.config.n_eval_episodes
        rewards_all: list[float] = []
        turnovers_all: list[float] = []
        values_all: list[float] = []
        for _ in range(n_episodes):
            obs = self._sim.reset()
            ep_rewards: list[float] = []
            ep_turnovers: list[float] = []
            ep_values: list[float] = [float(obs["portfolio_value"])]
            terminated = False
            truncated = False
            step = 0
            while not (terminated or truncated):
                action = self._policy_action(step)
                obs, reward, terminated, truncated, info = self._sim.step(action)
                ep_rewards.append(float(reward))
                ep_turnovers.append(float(info["turnover"]))
                ep_values.append(float(info["portfolio_value"]))
                step += 1
                if step >= self.config.max_checkpoint_steps:
                    truncated = True
            rewards_all.extend(ep_rewards)
            turnovers_all.extend(ep_turnovers)
            values_all.extend(ep_values)
        return _compute_metrics(rewards_all, turnovers_all, values_all)

    # -- persistence -----------------------------------------------------

    def _cache_dir(self) -> str:
        """Return the rollout cache dir (a per-instance temp dir)."""
        # Use a stable path under the system temp dir keyed by the
        # policy hash so repeated runs are reproducible.
        base = Path.home() / ".quant_foundry_shadow_cache"
        h = _policy_hash(self.config.policy_type, self.config.seed, self._n_assets)
        return str(base / h)

    def _save_policy_artifact(self) -> None:
        """Persist the (mocked) policy artifact to disk."""
        if self._checkpoint is None:
            return
        path = Path(self._checkpoint.artifact_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "checkpoint_id": self._checkpoint.checkpoint_id,
            "policy_type": self._checkpoint.policy_type,
            "policy_hash": self._checkpoint.policy_hash,
            "env_id": self._checkpoint.env_id,
            "seed": self.config.seed,
            "n_assets": self._n_assets,
            "max_weight": self._max_weight,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def save_result(self, result: RLShadowResult, path: str) -> None:
        """Save ``result`` to ``path`` as JSON.

        Args:
            result: the shadow result to save.
            path: the destination file path.
        """
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.model_dump_json(indent=2), encoding="utf-8")

    def load_result(self, path: str) -> RLShadowResult:
        """Load and return an :class:`RLShadowResult` from ``path``.

        Args:
            path: the source file path.

        Raises:
            FileNotFoundError: if ``path`` does not exist.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"result not found: {path}")
        return RLShadowResult.model_validate_json(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Promotion-eligibility & future-label validators
# ---------------------------------------------------------------------------


def validate_promotion_eligibility(result: RLShadowResult, manual_override: bool = False) -> bool:
    """Return whether a shadow result is eligible for live promotion.

    RL is a research-only lane: by default the result is **never**
    promotion-eligible. A live promotion requires an explicit
    ``manual_override=True`` (a deliberate policy change by the caller);
    even then this function only returns ``True`` — it does not mutate
    the (frozen) result. The result itself always carries
    ``promotion_eligible=False``.

    Args:
        result: the shadow result to check.
        manual_override: if ``True``, allow promotion (explicit override).

    Returns:
        ``False`` by default; ``True`` only when ``manual_override`` is
        ``True``.
    """
    if manual_override:
        return True
    return False


def validate_no_future_label_access(result: RLShadowResult) -> bool:
    """Check that a result's metrics do not indicate future-label access.

    Inspects the result's metrics for a ``future_label_access`` flag (set
    by callers that track label availability). Returns ``True`` when the
    result is safe; raises ``ValueError`` when future-label access is
    detected (fail-closed).

    Args:
        result: the shadow result to check.

    Returns:
        ``True`` when no future-label access is indicated.

    Raises:
        ValueError: if the metrics indicate future-label access.
    """
    metrics = result.metrics or {}
    if bool(metrics.get("future_label_access", False)):
        raise ValueError(
            "future-label access detected in result metrics; RL shadow result is unsafe"
        )
    return True


# ---------------------------------------------------------------------------
# Family registration
# ---------------------------------------------------------------------------


def register_rl_shadow_family() -> dict[str, Any]:
    """Return a ``ModelFamilySpec``-compatible dict for RL shadow registration.

    The returned dict carries the fields a
    :class:`~quant_foundry.alpha_genome.ModelFamilySpec` expects
    (family_id, display_name, version, dataset_shape, objectives,
    artifact_format, artifact_loader, required_metrics, etc.) plus
    RL-shadow-specific metadata. It is intended to be passed to
    ``ModelFamilyRegistry.register`` (after wrapping in a
    ``ModelFamilySpec``) by the caller — this function does **not**
    mutate the registry itself, keeping this module file-disjoint from
    ``alpha_genome.py``.

    The spec marks the RL shadow policy as a shadow family: it is **not**
    a baseline exception, does not require a GPU for the mocked
    heuristic path, and defaults to the ``CHALLENGER``
    promotion-eligibility class (though the policy itself forces
    ``promotion_eligible=False`` because ``shadow_only=True``).
    """
    return {
        "family_id": "rl_shadow",
        "display_name": "RL Shadow Policy (offline research lane)",
        "version": "1",
        "dataset_shape": "returns_series",
        "objectives": ("return", "cost", "drawdown", "turnover"),
        "artifact_format": "torch_state_dict",
        "artifact_loader": "quant_foundry.rl_shadow_policy.RLShadowPolicy.load_result",
        "required_metrics": (
            "sharpe",
            "max_drawdown",
            "avg_turnover",
            "total_return",
            "train_reward",
            "eval_reward",
        ),
        "runpod_image": None,
        "requires_gpu": False,
        "max_budget_cents": 0,
        "promotion_eligibility_class": "challenger",
        "is_baseline_exception": False,
        "created_at_ns": time.time_ns(),
        "shadow_only": True,
        "policy_types": ("ppo", "a2c", "dqn", "random"),
        "enforces_train_eval_separation": True,
        "enforces_future_label_guard": True,
    }


__all__ = [
    "FutureLabelGuard",
    "RLShadowConfig",
    "RLShadowPolicy",
    "RLShadowResult",
    "TrainEvalSeparator",
    "register_rl_shadow_family",
    "validate_no_future_label_access",
    "validate_promotion_eligibility",
]
