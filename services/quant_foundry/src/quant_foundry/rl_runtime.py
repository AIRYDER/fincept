"""
quant_foundry.rl_runtime — Reinforcement learning runtime (T-13.5).

This module provides a self-contained, importable reinforcement-learning
runtime for the quant foundry's GPU worker path. It is designed to be
**importable without torch / gymnasium / stable-baselines3 installed** —
all such imports are lazy and performed inside methods, so the module can
be imported on CPU-only machines (e.g. the local test suite) and only
fails when a heavy dependency is actually invoked.

Capabilities:

- :class:`RLImageSpec` — declarative spec for the ``trainer-gpu-rl``
  Docker image (base image, packages, healthcheck command).
- :class:`EnvironmentManifest` — versioned, hashed description of the
  deterministic market environment (Pydantic v2, frozen + ``extra='forbid'``).
- :class:`CostModel` — transaction-cost model with a deterministic hash.
- :class:`RolloutRecord` — typed artifact for a single rollout.
- :class:`PolicyCheckpoint` — typed artifact for a trained policy.
- :class:`DeterministicMarketSimulator` — a deterministic, seeded market
  simulator that applies target-weight actions, computes a reward
  (return - cost - drawdown penalty), and enforces risk limits fail-closed.
- :class:`RolloutManager` — saves / loads / lists rollout artifacts and
  detailed replay logs.
- :class:`RLHealthcheck` — healthcheck that probes the GPU, runs a
  simulator canary (reset + step), and round-trips a rollout artifact.

Design notes:

- **Lazy heavy imports.** ``torch`` / ``gymnasium`` /
  ``stable_baselines3`` imports happen inside methods, never at module
  top level. The Pydantic models and ``RLImageSpec`` can be constructed
  on a host without those packages.
- **No live trading authority.** The simulator trades synthetic prices
  only; it never touches real market data or produces tradeable decisions.
- **No secrets.** Configs carry only hyperparameters, hashes, and
  filesystem paths — never credentials.
- **Cost fails closed.** The healthcheck reports unhealthy when the GPU
  is unavailable or any probe raises; it never reports healthy on a
  partial probe. Risk-limit violations in the simulator raise
  ``ValueError`` rather than silently clipping.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Docker image spec
# ---------------------------------------------------------------------------


class RLImageSpec(BaseModel):
    """Declarative spec for the ``trainer-gpu-rl`` Docker image.

    Frozen + ``extra='forbid'`` for audit integrity. The spec is the
    source of truth for the image's base, packages, and healthcheck
    command; the Dockerfile in ``docker/trainer-gpu-rl/`` is kept in
    sync with it by review.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    image_name: str = "trainer-gpu-rl"
    base_image: str = "pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime"
    python_version: str = "3.12"
    packages: list[str] = Field(
        default_factory=lambda: [
            "torch==2.1.0",
            "numpy>=1.26",
            "pandas>=2.1",
            "pydantic>=2.7",
            "gymnasium>=0.29",
            "stable-baselines3>=2.2",
        ]
    )
    gpu_required: bool = True
    healthcheck_cmd: str = (
        'python -c "from quant_foundry.rl_runtime import '
        "RLHealthcheck; import sys; "
        'sys.exit(0 if RLHealthcheck().is_healthy() else 1)"'
    )
    supports_checkpoint_resume: bool = True
    rollout_cache_dir: str = "/opt/rollout_cache"


# ---------------------------------------------------------------------------
# Environment manifest
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


class EnvironmentManifest(BaseModel):
    """Versioned, hashed description of a deterministic market environment.

    Frozen + ``extra='forbid'`` for audit integrity. The ``env_hash`` and
    ``cost_model_hash`` are SHA-256 digests (64-char lowercase hex) of the
    environment and cost-model configs respectively, used to guarantee
    that a rollout is reproducible from the manifest alone.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    env_id: str
    env_version: str
    env_hash: str
    cost_model_hash: str
    n_assets: int
    n_timesteps: int
    reward_components: list[str]
    risk_limits: dict[str, float]
    created_at: str

    @field_validator("env_hash", "cost_model_hash")
    @classmethod
    def _validate_hex64(cls, v: str) -> str:
        if not _is_hex64(v):
            raise ValueError("hash must be a 64-character lowercase hex SHA-256 digest")
        return v

    @field_validator("n_assets")
    @classmethod
    def _validate_n_assets(cls, v: int) -> int:
        if v < 1:
            raise ValueError("n_assets must be >= 1")
        return v

    @field_validator("n_timesteps")
    @classmethod
    def _validate_n_timesteps(cls, v: int) -> int:
        if v < 1:
            raise ValueError("n_timesteps must be >= 1")
        return v


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------


class CostModel(BaseModel):
    """Transaction-cost model with a deterministic SHA-256 hash.

    Frozen + ``extra='forbid'`` for audit integrity. All cost parameters
    are non-negative. ``cost_hash`` is a deterministic SHA-256 digest of
    the model's cost parameters (commission, slippage, market impact) so
    that two cost models with identical parameters share a hash.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    commission_bps: float = 0.0
    slippage_bps: float = 0.0
    market_impact: float = 0.0
    cost_hash: str

    @field_validator("commission_bps", "slippage_bps", "market_impact")
    @classmethod
    def _validate_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("cost parameters must be >= 0")
        return v

    @field_validator("cost_hash")
    @classmethod
    def _validate_cost_hash(cls, v: str) -> str:
        if not _is_hex64(v):
            raise ValueError("cost_hash must be a 64-character lowercase hex SHA-256 digest")
        return v

    @staticmethod
    def compute_hash(
        commission_bps: float,
        slippage_bps: float,
        market_impact: float,
    ) -> str:
        """Return the deterministic SHA-256 hash for the given parameters.

        The hash is computed over a canonical JSON encoding of the three
        cost parameters (rounded to 12 decimal places to avoid float
        representation drift).
        """
        payload = json.dumps(
            {
                "commission_bps": round(float(commission_bps), 12),
                "slippage_bps": round(float(slippage_bps), 12),
                "market_impact": round(float(market_impact), 12),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Rollout record
# ---------------------------------------------------------------------------


class RolloutRecord(BaseModel):
    """Typed artifact for a single rollout.

    Frozen + ``extra='forbid'`` for audit integrity. ``actions`` is a
    list of per-step action vectors (each of length ``n_assets``);
    ``rewards`` is a list of per-step scalar rewards; ``cumulative_reward``
    is the sum of ``rewards``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rollout_id: str
    env_id: str
    policy_hash: str
    n_steps: int
    actions: list[list[float]]
    rewards: list[float]
    cumulative_reward: float
    terminated: bool
    truncated: bool
    metadata: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Policy checkpoint
# ---------------------------------------------------------------------------


class PolicyCheckpoint(BaseModel):
    """Typed artifact for a trained policy checkpoint.

    Frozen + ``extra='forbid'`` for audit integrity. ``policy_hash`` is a
    SHA-256 digest of the policy parameters; ``env_id`` is the environment
    the policy was trained on; ``artifact_path`` is the filesystem path
    to the serialized policy artifact.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoint_id: str
    policy_type: str
    policy_hash: str
    env_id: str
    training_steps: int
    artifact_path: str
    created_at: str

    @field_validator("policy_hash")
    @classmethod
    def _validate_policy_hash(cls, v: str) -> str:
        if not _is_hex64(v):
            raise ValueError("policy_hash must be a 64-character lowercase hex SHA-256 digest")
        return v

    @field_validator("training_steps")
    @classmethod
    def _validate_training_steps(cls, v: int) -> int:
        if v < 0:
            raise ValueError("training_steps must be >= 0")
        return v


# ---------------------------------------------------------------------------
# Deterministic market simulator
# ---------------------------------------------------------------------------


class DeterministicMarketSimulator:
    """A deterministic, seeded market simulator for RL rollouts.

    The simulator maintains a synthetic price series for ``n_assets``
    assets over ``n_timesteps`` steps. Prices are generated deterministically
    from ``seed`` so that two simulators with the same manifest + seed
    produce identical series. Actions are target portfolio weights; the
    reward is ``return - cost - drawdown_penalty``. Risk limits are
    enforced fail-closed: a violation raises ``ValueError``.

    All numpy / torch / gymnasium imports are lazy (inside methods), so
    the simulator can be constructed on a host without those packages
    (a pure-Python fallback path is used for the synthetic price series).
    """

    def __init__(
        self,
        env_manifest: EnvironmentManifest,
        cost_model: CostModel,
        seed: int = 42,
    ) -> None:
        self.env_manifest = env_manifest
        self.cost_model = cost_model
        self.seed = seed
        self._n_assets = env_manifest.n_assets
        self._n_timesteps = env_manifest.n_timesteps
        self._risk_limits = dict(env_manifest.risk_limits)
        self._max_weight = self._risk_limits.get("max_weight", 1.0)
        self._max_turnover = self._risk_limits.get("max_turnover", float("inf"))
        self._prices: list[list[float]] = []
        self._current_step = 0
        self._prev_weights: list[float] = [1.0 / self._n_assets for _ in range(self._n_assets)]
        self._peak_value = 1.0
        self._portfolio_value = 1.0
        self._rng: random.Random | None = None
        self.reset()

    # -- price generation -------------------------------------------------

    def _generate_prices(self) -> list[list[float]]:
        """Generate the deterministic synthetic price series.

        Prices start at 1.0 for every asset and follow a deterministic
        random walk driven by ``self.seed``. Returns a list of length
        ``n_timesteps + 1`` (the initial price row plus one row per step).
        """
        rng = random.Random(self.seed)
        prices: list[list[float]] = []
        row = [1.0 for _ in range(self._n_assets)]
        prices.append(list(row))
        for _ in range(self._n_timesteps):
            for i in range(self._n_assets):
                # Deterministic shock in [-0.02, 0.02].
                shock = (rng.random() - 0.5) * 0.04
                row[i] = max(row[i] * (1.0 + shock), 1e-6)
            prices.append(list(row))
        return prices

    # -- gym-style API ----------------------------------------------------

    def reset(self) -> dict:
        """Reset the simulator and return the initial observation.

        The observation dict contains ``prices`` (current price row),
        ``weights`` (current portfolio weights), ``step`` (0), and
        ``portfolio_value`` (1.0).
        """
        self._prices = self._generate_prices()
        self._current_step = 0
        self._prev_weights = [1.0 / self._n_assets for _ in range(self._n_assets)]
        self._peak_value = 1.0
        self._portfolio_value = 1.0
        return self._observation()

    def _observation(self) -> dict:
        """Return the current observation dict."""
        return {
            "prices": list(self._prices[self._current_step]),
            "weights": list(self._prev_weights),
            "step": self._current_step,
            "portfolio_value": self._portfolio_value,
        }

    def validate_action(self, action: list[float]) -> None:
        """Validate a target-weight action.

        Checks that the action length matches ``n_assets``, that weights
        sum to ~1.0 (within ``1e-4`` tolerance), and that no weight
        exceeds ``risk_limits["max_weight"]``. Raises ``ValueError`` if
        any check fails.
        """
        if not isinstance(action, (list, tuple)):
            raise ValueError("action must be a list of floats")
        if len(action) != self._n_assets:
            raise ValueError(
                f"action length {len(action)} does not match n_assets {self._n_assets}"
            )
        total = sum(float(w) for w in action)
        if abs(total - 1.0) > 1e-4:
            raise ValueError(f"action weights must sum to 1.0 (within 1e-4), got {total}")
        for i, w in enumerate(action):
            if abs(float(w)) > self._max_weight + 1e-9:
                raise ValueError(f"weight {w} at index {i} exceeds max_weight {self._max_weight}")

    def step(self, action: list[float]) -> tuple[dict, float, bool, bool, dict]:
        """Apply a target-weight action and advance one step.

        Computes the per-asset return from the price series, the portfolio
        return as the weighted sum, the transaction cost from the weight
        change (turnover), a drawdown penalty, and the net reward
        ``return - cost - drawdown_penalty``. Risk limits are enforced
        fail-closed via :meth:`validate_action` and a turnover check.

        Returns the ``(observation, reward, terminated, truncated, info)``
        tuple in the gymnasium convention.
        """
        self.validate_action(action)

        # Turnover = sum of absolute weight changes.
        turnover = sum(abs(float(action[i]) - self._prev_weights[i]) for i in range(self._n_assets))
        if turnover > self._max_turnover + 1e-9:
            raise ValueError(f"turnover {turnover} exceeds max_turnover {self._max_turnover}")

        next_step = self._current_step + 1
        prev_prices = self._prices[self._current_step]
        next_prices = self._prices[next_step] if next_step < len(self._prices) else prev_prices

        # Per-asset simple return.
        asset_returns = [(next_prices[i] / prev_prices[i]) - 1.0 for i in range(self._n_assets)]
        # Portfolio return = weighted sum of asset returns.
        portfolio_return = sum(float(action[i]) * asset_returns[i] for i in range(self._n_assets))

        # Transaction cost in return units.
        commission = self.cost_model.commission_bps / 1e4 * turnover
        slippage = self.cost_model.slippage_bps / 1e4 * turnover
        impact = self.cost_model.market_impact * (turnover**2)
        cost = commission + slippage + impact

        # Update portfolio value.
        self._portfolio_value = self._portfolio_value * (1.0 + portfolio_return - cost)
        if self._portfolio_value > self._peak_value:
            self._peak_value = self._portfolio_value
        drawdown = (self._peak_value - self._portfolio_value) / max(self._peak_value, 1e-12)
        drawdown_penalty = 0.5 * drawdown

        reward = portfolio_return - cost - drawdown_penalty

        self._prev_weights = [float(w) for w in action]
        self._current_step = next_step

        terminated = self._portfolio_value <= 0.0
        truncated = self._current_step >= self._n_timesteps

        info = {
            "portfolio_return": portfolio_return,
            "cost": cost,
            "turnover": turnover,
            "drawdown": drawdown,
            "drawdown_penalty": drawdown_penalty,
            "portfolio_value": self._portfolio_value,
            "asset_returns": asset_returns,
        }
        return self._observation(), reward, terminated, truncated, info


# ---------------------------------------------------------------------------
# Rollout manager
# ---------------------------------------------------------------------------


class RolloutManager:
    """Saves, loads, and lists rollout artifacts.

    Rollouts are written to ``cache_dir`` as ``rollout_{rollout_id}.json``
    files containing the serialized :class:`RolloutRecord`. A detailed
    replay log (step-by-step) can additionally be written to an explicit
    path via :meth:`save_replay_log`.

    All file I/O uses the standard library only (no heavy deps), so the
    manager works on any host.
    """

    def __init__(self, cache_dir: str) -> None:
        self.cache_dir = cache_dir
        self._dir = Path(cache_dir)

    def _path_for(self, rollout_id: str) -> Path:
        """Return the cache path for a given rollout id."""
        safe = rollout_id.replace("/", "_").replace("\\", "_")
        return self._dir / f"rollout_{safe}.json"

    def save_rollout(self, rollout: RolloutRecord) -> str:
        """Save ``rollout`` to the cache dir and return its file path.

        Creates ``cache_dir`` if it does not exist.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path_for(rollout.rollout_id)
        payload = rollout.model_dump_json(indent=2)
        path.write_text(payload, encoding="utf-8")
        return str(path)

    def load_rollout(self, rollout_id: str) -> RolloutRecord:
        """Load and return the rollout with the given id.

        Raises:
            FileNotFoundError: if no rollout with that id is cached.
        """
        path = self._path_for(rollout_id)
        if not path.exists():
            raise FileNotFoundError(f"rollout not found: {rollout_id}")
        return RolloutRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list_rollouts(self) -> list[str]:
        """Return a sorted list of cached rollout ids."""
        if not self._dir.exists():
            return []
        ids: list[str] = []
        for f in self._dir.glob("rollout_*.json"):
            stem = f.stem.replace("rollout_", "")
            ids.append(stem)
        return sorted(ids)

    def save_replay_log(self, rollout: RolloutRecord, path: str) -> None:
        """Save a detailed step-by-step replay log to ``path``.

        The replay log is a JSON file with a ``rollout_id``, ``env_id``,
        ``policy_hash``, and a ``steps`` list. Each step entry contains
        the index, action vector, reward, and running cumulative reward.
        """
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        cumulative = 0.0
        steps: list[dict[str, Any]] = []
        for i, (action, reward) in enumerate(zip(rollout.actions, rollout.rewards, strict=False)):
            cumulative += float(reward)
            steps.append(
                {
                    "step": i,
                    "action": list(action),
                    "reward": float(reward),
                    "cumulative_reward": cumulative,
                }
            )
        payload = {
            "rollout_id": rollout.rollout_id,
            "env_id": rollout.env_id,
            "policy_hash": rollout.policy_hash,
            "n_steps": rollout.n_steps,
            "terminated": rollout.terminated,
            "truncated": rollout.truncated,
            "cumulative_reward": rollout.cumulative_reward,
            "steps": steps,
        }
        out.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------


class RLHealthcheck:
    """Healthcheck for the RL runtime.

    Probes the GPU via :func:`check_gpu` (reused from
    ``tabular_neural_runtime``), runs a simulator canary (reset + step
    with a valid action), and round-trips a rollout artifact through a
    :class:`RolloutManager`. Used by the GPU worker's ``HEALTHCHECK``
    step to fail fast when the runtime is broken or the GPU is missing.
    """

    def __init__(self, timeout_seconds: int = 60) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds

    def run(self) -> dict[str, Any]:
        """Run the healthcheck and return a status dict.

        The dict contains:

        - ``healthy`` (bool): overall health.
        - ``gpu`` (dict): serialized :class:`GPUStatus`.
        - ``simulator_canary`` (bool): whether reset + step succeeded.
        - ``rollout_roundtrip`` (bool): whether the save/load round-trip
          succeeded.
        - ``error`` (str | None): error message if the check failed.
        - ``duration_seconds`` (float): wall-clock duration.
        """
        from quant_foundry.tabular_neural_runtime import check_gpu

        start = time.perf_counter()
        result: dict[str, Any] = {
            "healthy": False,
            "gpu": None,
            "simulator_canary": False,
            "rollout_roundtrip": False,
            "error": None,
            "duration_seconds": 0.0,
        }

        try:
            gpu_status = check_gpu()
            result["gpu"] = gpu_status.model_dump()
        except Exception as exc:  # pragma: no cover - defensive
            result["error"] = f"gpu probe failed: {exc}"
            result["duration_seconds"] = time.perf_counter() - start
            return result

        # Simulator canary: build a tiny manifest + cost model, reset,
        # and step with a uniform-weight action.
        try:
            env_hash = "a" * 64
            cost_hash = "b" * 64
            manifest = EnvironmentManifest(
                env_id="canary-env",
                env_version="0.1.0",
                env_hash=env_hash,
                cost_model_hash=cost_hash,
                n_assets=2,
                n_timesteps=3,
                reward_components=["return", "cost", "drawdown", "turnover"],
                risk_limits={"max_weight": 1.0, "max_turnover": 1.0},
                created_at=datetime.now(UTC).isoformat(),
            )
            cost_model = CostModel(
                model_id="canary-cost",
                commission_bps=0.0,
                slippage_bps=0.0,
                market_impact=0.0,
                cost_hash=cost_hash,
            )
            sim = DeterministicMarketSimulator(manifest, cost_model, seed=42)
            obs = sim.reset()
            assert "prices" in obs
            action = [0.5, 0.5]
            _o, _r, _t, _tr, _info = sim.step(action)
            result["simulator_canary"] = True
        except Exception as exc:  # pragma: no cover - defensive
            result["error"] = f"simulator canary failed: {exc}"
            result["duration_seconds"] = time.perf_counter() - start
            return result

        # Rollout save/load round-trip in a temp dir.
        try:
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                mgr = RolloutManager(cache_dir=tmp)
                rollout = RolloutRecord(
                    rollout_id="canary-rollout",
                    env_id="canary-env",
                    policy_hash="c" * 64,
                    n_steps=1,
                    actions=[[0.5, 0.5]],
                    rewards=[0.0],
                    cumulative_reward=0.0,
                    terminated=False,
                    truncated=True,
                )
                saved_path = mgr.save_rollout(rollout)
                assert Path(saved_path).exists()
                loaded = mgr.load_rollout("canary-rollout")
                assert loaded.rollout_id == rollout.rollout_id
                assert "canary-rollout" in mgr.list_rollouts()
                result["rollout_roundtrip"] = True
        except Exception as exc:  # pragma: no cover - defensive
            result["error"] = f"rollout roundtrip failed: {exc}"
            result["duration_seconds"] = time.perf_counter() - start
            return result

        gpu_ok = bool(result["gpu"].get("available")) if result["gpu"] else False
        result["healthy"] = bool(
            gpu_ok
            and result["simulator_canary"]
            and result["rollout_roundtrip"]
            and result["error"] is None
        )
        result["duration_seconds"] = time.perf_counter() - start
        return result

    def is_healthy(self) -> bool:
        """Return ``True`` if the GPU is available and all probes succeed.

        Note: on a CPU-only host this returns ``False`` because the GPU is
        not available. The healthcheck is intended for the GPU worker
        container, where a missing GPU is a hard failure.
        """
        status = self.run()
        return bool(status.get("healthy"))


__all__ = [
    "CostModel",
    "DeterministicMarketSimulator",
    "EnvironmentManifest",
    "PolicyCheckpoint",
    "RLHealthcheck",
    "RLImageSpec",
    "RolloutManager",
    "RolloutRecord",
]
